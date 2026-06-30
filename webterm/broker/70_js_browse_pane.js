        // ---- reusable single browse-pane component (#93) ------------------
        // ONE directory-browser kernel shared by the editor's Open/Save/choose-
        // folder dialog (core, openFileDialog in 68) and the file manager's two
        // panes (mods/file-manager). Both used to carry a near-clone of the same
        // walk /file/list -> build 📁/📄 rows -> navigate/select logic; #93
        // extracts it here, in CORE, so the editor dialog still works mods-off
        // and the duplication can't silently drift apart again. (The FM color-
        // scheme unification is split to #94 — this is structural only, which is
        // why each consumer keeps its OWN row CSS class names via `classes`.)
        //
        // The component is strictly HOST- and I/O-AGNOSTIC: it never references
        // hosts, fileApiPost, ctx, auth, or persistence. It OWNS the row markup
        // (icon/name/size, classes injected so each consumer keeps its own CSS),
        // the '..' row, single-click .sel selection, the single-vs-double-click
        // activation policy, current-cwd tracking, the navigate/await sequencing
        // (a per-instance `seq`), and a synchronously-set `disposed` flag.
        //
        // The CONSUMER OWNS, injected through the hooks below: all host
        // resolution + auth prompts + not_found retry policy + every
        // saveAppWindow. This is what keeps persistence byte-identical and the
        // fail-closed host semantics intact across the extraction.
        //
        //   listDir(path, token)   async — the host-routed /file/list accessor.
        //   snapshot()  -> token   capture request identity at navigate time.
        //   isCurrent(token) -> b  the consumer's semantic staleness check, run
        //                          AFTER the await and BEFORE any side-effect.
        //   onDirChanged(cwd, res) successful, non-stale list (canonical cwd).
        //   onListError(res, info) non-stale failed list; info = {requestedPath,
        //                          host, navigate, placeholder}.
        //   onSelect(entry, child) a row was single-selected (.sel applied).
        //   onActivateFile(child, entry)  a FILE row was activated.
        //   decorateRow({rowEl, entry, childPath, kind, select})  per entry row.
        //
        // `seq` handles "a newer navigate superseded me"; `isCurrent(token)`
        // handles "the consumer's state no longer matches the request" (host
        // switch / dir drift) — together a SUPERSET of the FM's old 4-clause
        // guard, so correctness stays explicit semantic state-validation, not a
        // fragile "everything must go through the component" convention.
        //
        // createBrowsePane(opts) -> { navigate, reload, goParent,
        //   activateSelected, getCwd, getSelected, destroy }
        function createBrowsePane(opts) {
            const listEl = opts.listEl;
            const classes = opts.classes;
            // Files greyed + inert (the dialog's folder-picker 'dir' mode).
            const filesInteractive = opts.filesInteractive !== false;
            // Gesture that ENTERS a directory: the dialog navigates on a single
            // click; the FM selects on single and enters on double.
            const dirActivateOn = opts.dirActivateOn === 'single'
                ? 'single' : 'double';

            // Per-instance navigate sequence + a synchronously-set disposed flag.
            // Both are checked AFTER the await so a superseded / torn-down pane
            // can never paint (codex #1/#9): destroy() flips disposed the same
            // tick, before any in-flight list resolves.
            let seq = 0;
            let disposed = false;
            let cwd = '';            // server-canonical absolute cwd (#35)
            let lastResult = null;   // last successful list (for goParent)
            let selected = null;     // {kind, child, entry, rowEl} | null

            // Clear the list and show ONE neutral row — the consumer's error /
            // sign-in / no-host placeholder (handed to onListError).
            const placeholder = (text) => {
                listEl.innerHTML = '';
                const row = document.createElement('div');
                row.className = classes.row;
                row.textContent = text;
                listEl.appendChild(row);
            };

            // Component-owned single selection: drop any prior .sel, mark this
            // row, and record what's selected (getSelected / activateSelected).
            const applySelect = (rowEl, sel) => {
                listEl.querySelectorAll('.' + classes.row + '.sel')
                    .forEach(r => r.classList.remove('sel'));
                rowEl.classList.add('sel');
                selected = sel;
            };

            const navigate = async (path) => {
                const mySeq = ++seq;
                const token = opts.snapshot();
                const res = await opts.listDir(path, token);
                // BEFORE any side-effect: disposed (this pane), seq (a newer
                // navigate), or the consumer's own staleness check (FM host /
                // dir drift). Any of them -> drop this reply silently.
                if (disposed || mySeq !== seq || !opts.isCurrent(token)) return;
                if (!res || !res.ok) {
                    opts.onListError(res, {
                        requestedPath: path, host: token.host,
                        navigate: navigate, placeholder: placeholder });
                    return;
                }
                cwd = res.cwd || '';          // absolute path (#35)
                lastResult = res;
                selected = null;              // rows are rebuilt below
                opts.onDirChanged(cwd, res);
                listEl.innerHTML = '';
                // '..' parent row (only when the server reports a parent).
                if (res.parent !== null && res.parent !== undefined) {
                    const parentPath = res.parent;
                    const up = document.createElement('div');
                    up.className = classes.row;
                    up.innerHTML = '<span class="' + classes.icon + '">📁</span>'
                        + '<span class="' + classes.name + '">..</span>'
                        + '<span class="' + classes.size + '"></span>';
                    const sel = { kind: 'parent', child: parentPath,
                                  entry: null, rowEl: up };
                    if (dirActivateOn === 'single') {
                        up.addEventListener('click', () => navigate(parentPath));
                    } else {
                        up.addEventListener('click', () => {
                            applySelect(up, sel);
                            if (opts.onSelect) opts.onSelect(null, parentPath);
                        });
                        up.addEventListener('dblclick',
                            () => navigate(parentPath));
                    }
                    if (opts.decorateRow) {
                        opts.decorateRow({ rowEl: up, entry: null,
                            childPath: parentPath, kind: 'parent',
                            select: () => applySelect(up, sel) });
                    }
                    listEl.appendChild(up);
                }
                for (const ent of (res.entries || [])) {
                    const row = document.createElement('div');
                    row.className = classes.row;
                    const icon = ent.type === 'dir' ? '📁' : '📄';
                    row.innerHTML = '<span class="' + classes.icon + '">' + icon
                        + '</span><span class="' + classes.name + '"></span>'
                        + '<span class="' + classes.size + '">'
                        + (ent.type === 'dir' ? '' : fmtSize(ent.size))
                        + '</span>';
                    row.querySelector('.' + classes.name).textContent = ent.name;
                    const child = joinNative(cwd, ent.name);
                    const kind = ent.type === 'dir' ? 'dir' : 'file';
                    const sel = { kind: kind, child: child,
                                  entry: ent, rowEl: row };
                    if (ent.type === 'dir') {
                        if (dirActivateOn === 'single') {
                            row.addEventListener('click', () => navigate(child));
                        } else {
                            row.addEventListener('click', () => {
                                applySelect(row, sel);
                                if (opts.onSelect) opts.onSelect(ent, child);
                            });
                            row.addEventListener('dblclick',
                                () => navigate(child));
                        }
                    } else if (!filesInteractive) {
                        // Folder picker: files are greyed + inert (you can only
                        // navigate folders and commit the shown one).
                        row.classList.add('disabled');
                        row.style.opacity = '0.4';
                        row.style.cursor = 'default';
                    } else {
                        row.addEventListener('click', () => {
                            applySelect(row, sel);
                            if (opts.onSelect) opts.onSelect(ent, child);
                        });
                        row.addEventListener('dblclick', () => {
                            applySelect(row, sel);
                            if (opts.onActivateFile) opts.onActivateFile(child, ent);
                        });
                    }
                    if (opts.decorateRow) {
                        opts.decorateRow({ rowEl: row, entry: ent,
                            childPath: child, kind: kind,
                            select: () => applySelect(row, sel) });
                    }
                    listEl.appendChild(row);
                }
            };

            return {
                navigate: navigate,
                reload: () => navigate(cwd),
                goParent: () => {
                    if (lastResult && lastResult.parent !== null
                        && lastResult.parent !== undefined) {
                        navigate(lastResult.parent);
                    }
                },
                // Activate whatever is selected (FM toolbar Open + Enter): a file
                // opens, a dir/'..' navigates — replaces the old synthetic
                // dispatchEvent(dblclick) (codex #8).
                activateSelected: () => {
                    if (!selected) return;
                    if (selected.kind === 'file') {
                        if (filesInteractive && opts.onActivateFile) {
                            opts.onActivateFile(selected.child, selected.entry);
                        }
                    } else {
                        navigate(selected.child);   // dir or '..'
                    }
                },
                getCwd: () => cwd,
                getSelected: () => (selected ? selected.child : null),
                destroy: () => { disposed = true; },
            };
        }

