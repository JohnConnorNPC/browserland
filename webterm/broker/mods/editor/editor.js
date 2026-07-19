        // ---- mod: text editor (S10 / #83) ---------------------------------
        // The text-editor app window, extracted from core as a
        // ctx.registerWindowKind mod (#83). It was a core built-in in #80's
        // window-kind registry; the same spec now ships here and registers
        // through the mod's ctx, so a text editor is a first-class window
        // everywhere the registry is consulted (open / serialize / restore /
        // close / the (+) launch menu). Only its OWNER moved from core to a mod.
        //
        // What moved here, verbatim except the I/O swap below: the shared
        // sticky-note / text-editor builder openNoteOrEditorWindow (the editor
        // half -- CodeMirror, multi-tab AGENTS.md/CLAUDE.md docs, dirty-save,
        // find, line numbers, host/folder pickers -- plus the note half it has
        // always shared), the CodeMirror 6 lazy loader (mods/editor/codemirror.js),
        // and the (+) launcher launchTextEditor. (#120: the AGENTS.md hooks
        // openAgentDocsWindow + openAgentsMdEditor were since split out into the
        // agent-docs mod (mods/agent-docs/), which requires this mod and still
        // drives the shared docs/Sections/template machinery below as hoisted
        // free identifiers.) All are
        // top-level `function` declarations, so they HOIST across the one
        // concatenated <script> and stay reachable from core regardless of
        // mods_enabled -- the same posture the help/sticky mods use.
        //
        // PERSISTENCE IS BYTE-IDENTICAL (the issue's hard requirement): the spec
        // reuses the EXACT core serializer serializeAppWindow (still core, shared
        // with file-manager + the sticky mod), so every serialized field --
        // filePath, wrap, lineNums, startDir, docs, activeTab, agentsMdCwd,
        // fileHostId -- round-trips unchanged through webterm:appwindows:v1.
        //
        // FILE I/O rides ctx.file (#82): every /file/* call goes through the
        // editorFile() accessor (below) instead of fileApiPost directly, so all
        // filesystem access is funneled through the one reviewed capability. The
        // host routing is byte-identical -- each call passes the host *id* of the
        // host object the editor already resolved (H.id), which ctx.file /
        // _modFileApi re-resolve to the SAME cached object.
        //
        // mods_enabled=false posture (same as every extracted mod): the
        // text-editor kind is simply not REGISTERED, so the (+) "Text editor"
        // launcher disappears. But the builder + AGENTS hooks are hoisted, so a
        // restored editor (openAppWindow's unknown-kind fallback ->
        // openNoteOrEditorWindow) and the terminal button still open, and
        // editorFile() falls back to the SAME core _modFileApi ctx.file wraps --
        // so reads/writes still work. Only the launcher + the kind's
        // registry-owned serialize/menu ride the mod; the broker mods kill switch
        // is the deliberate "I turned the mods off" degradation.

        // ---- ctx.file accessor --------------------------------------------
        // The single choke point every editor /file/* call flows through. init()
        // stashes the per-mod ctx.file on editorFile.cap (a function property --
        // no TDZ, the window.__mods / localInfo._p no-TDZ pattern), which the
        // hoisted builder closures read via editorFile(). With mods off (init
        // never ran) it degrades to a literal mirror of ctx.file's read/write/
        // list over the hoisted core _modFileApi (the SAME plumbing ctx.file
        // wraps, identical request bodies + fail-closed host-id routing), so the
        // editor's I/O is identical mods on or off. opts.host is a host-id.
        function editorFile() {
            return editorFile.cap || {
                read: function (path, opts) {
                    const body = { path: path };
                    if (opts && opts.b64 === true) body.b64 = true;
                    return _modFileApi('/file/read', body, opts);
                },
                write: function (path, content, opts) {
                    const body = { path: path, content: content };
                    if (opts && opts.encoding) body.encoding = opts.encoding;
                    return _modFileApi('/file/write', body, opts);
                },
                list: function (path, opts) {
                    return _modFileApi('/file/list', { path: path || '' }, opts);
                },
            };
        }

        // The sticky-note / text-editor builder — the original openAppWindow body,
        // extracted (#80/S7) so it can be the registry `factory` for both kinds (and
        // the unknown-kind default). The notes/editor DOM is delicate; the other app
        // kinds have their own factories (openFileManagerWindow, openTaskManager-
        // Window, openControlPanelWindow, openHelpWindow). Reached only via
        // openAppWindow, which already ran the dedup-by-id check, so this rebuilds
        // unconditionally.
        function openNoteOrEditorWindow(appData) {
            const id = String(appData.id);
            // Legacy single-doc AGENTS.md record (agentsMdCwd, no docs): a build
            // before the tabbed agent-docs window. Upgrade it on reopen by reading
            // AGENTS.md + CLAUDE.md fresh, preserving the stored geom/color/tiling.
            // openAgentDocsWindow (now in the agent-docs mod, mods/agent-docs/, but
            // a hoisted free identifier reachable here regardless of that mod's
            // enabled state — #120) re-enters openAppWindow with a `docs` array, so
            // this branch never recurses. Async — the window appears a moment later.
            if (appData.appKind === 'text-editor' && appData.agentsMdCwd
                && !appData.docs) {
                openAgentDocsWindow({
                    id, cwd: String(appData.agentsMdCwd),
                    fileHostId: appData.fileHostId || 'local',
                    geom: appData.geom, color: appData.color,
                    locked: appData.locked, floatGeom: appData.floatGeom,
                });
                return null;
            }
            // Multi-tab "Agent docs" window: appData.docs holds the per-tab file
            // buffers ([AGENTS.md, CLAUDE.md]); a synthetic Sections tab is added.
            // Drives one shared editor surface from an "active doc" (win.tabs).
            const docs = Array.isArray(appData.docs) && appData.docs.length
                ? appData.docs.map((d, i) => ({
                    kind: 'file',
                    name: typeof d.name === 'string' ? d.name
                        : (i === 0 ? 'AGENTS.md' : 'CLAUDE.md'),
                    filePath: d.filePath != null ? String(d.filePath) : null,
                    content: typeof d.content === 'string' ? d.content : '',
                    dirty: !!d.dirty,
                    wrap: d.wrap !== undefined ? !!d.wrap : true,
                    lineNums: d.lineNums !== undefined ? !!d.lineNums : true,
                    isAgents: d.isAgents !== undefined ? !!d.isAgents
                        : (d.name === 'AGENTS.md' || i === 0),
                    // Source encoding (#97) so a restored UTF-16/cp1252 tab
                    // saves back in its own encoding (default utf-8).
                    encoding: typeof d.encoding === 'string' ? d.encoding
                        : 'utf-8',
                    cmState: null, scrollTop: 0, scrollLeft: 0,
                  }))
                : null;
            const tabs = docs ? docs.concat([{ kind: 'sections' }]) : null;
            // Which tab is active on open (restored windows persist it); clamp.
            let activeTab = 0;
            if (tabs) {
                const at = Number(appData.activeTab);
                activeTab = (Number.isInteger(at) && at >= 0 && at < tabs.length)
                    ? at : 0;
            }
            // The file doc the editor surface shows on open. When the persisted
            // active tab is the Sections tab, bind the editor to the AGENTS doc
            // (kept hidden) so CM still has a real buffer to mount against.
            const initialDoc = tabs
                ? (tabs[activeTab].kind === 'file' ? tabs[activeTab]
                   : (docs.find(d => d.isAgents) || docs[0]))
                : null;
            const sectionsActiveOnOpen = !!(tabs
                && tabs[activeTab].kind === 'sections');

            const appKind = appData.appKind === 'text-editor'
                ? 'text-editor' : 'sticky-note';
            const isNote = appKind === 'sticky-note';
            const sid = isNote ? 'note' : 'txt';
            const title = appData.title || (isNote ? 'Sticky Note' : 'Text Editor');
            const geom = clampGeom(appData.geom || appDefaultGeom(appKind));
            const color = normalizeHex(appData.color
                || (isNote ? NOTE_DEFAULT_ACCENT : defaultColor(id)));
            // New notes default to pinned (locked); a restored doc keeps its
            // saved choice. Editors default to word-wrap on.
            const locked = appData.locked !== undefined ? !!appData.locked : true;
            // Sticky notes are always-on-top by default (#95); a restored note
            // keeps its saved choice. `!== false` → a note with no/true `pinned`
            // stays pinned (backward-compatible for pre-#95 records). Note-only;
            // false for editors so it never lifts them into the note z-tier.
            const pinned = isNote ? (appData.pinned !== false) : false;
            // Word wrap applies to BOTH editors and notes (#19); both default on.
            const wrap = docs ? !!initialDoc.wrap
                : (appData.wrap !== undefined ? !!appData.wrap : true);
            // Per-note text size (#19): clamped, default 13px (the CSS base).
            const NOTE_FONT_MIN = 9, NOTE_FONT_MAX = 28, NOTE_FONT_DEFAULT = 13;
            // Explicit finite check so a corrupt 0/"0"/NaN clamps sensibly
            // (`parseInt(...) || default` would turn a finite 0 into the default).
            const _fsRaw = parseInt(appData.fontSize, 10);
            const noteFontSize = Number.isFinite(_fsRaw)
                ? Math.max(NOTE_FONT_MIN, Math.min(NOTE_FONT_MAX, _fsRaw))
                : NOTE_FONT_DEFAULT;

            // Shared chrome (#79): the .term-window shell + title bar (_ / ×) +
            // the eight resize handles come from the window-runtime factory. The
            // editor hangs its kind-specific extras (note paper/fg, the rename
            // hint, the note A-/A+/wrap buttons, the color picker, the toolbar +
            // body) onto the returned refs.
            const chrome = buildAppChrome({
                id,
                appClass: isNote ? 'app-note' : 'app-editor',
                badge: '#' + sid,
                geom, color, locked, title,
            });
            const { dom, titleBar, titleText, minBtn } = chrome;
            // A sticky note's paper/fg are DERIVED from the chosen accent.
            if (isNote) {
                const sw0 = noteSwatchFor(color);
                dom.style.setProperty('--note-paper', sw0.paper);
                dom.style.setProperty('--note-fg', sw0.fg);
            }
            titleText.title = 'double-click to rename';

            // Sticky-note controls (#19): smaller / larger text + word-wrap
            // toggle, inserted into the title bar BEFORE the minimize button
            // (notes have no editor toolbar). Wired below once `win` exists.
            let noteFontDownBtn = null, noteFontUpBtn = null, noteWrapBtn = null,
                notePinBtn = null;
            if (isNote) {
                const mkNoteBtn = (label, ttl) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'tb-btn';
                    b.textContent = label;
                    b.title = ttl;
                    b.setAttribute('aria-label', ttl);
                    return b;
                };
                noteFontDownBtn = mkNoteBtn('A-', 'smaller text');
                noteFontUpBtn = mkNoteBtn('A+', 'larger text');
                noteWrapBtn = mkNoteBtn('↩', 'toggle word wrap');
                // Always-on-top toggle (#95). ▲ pinned / △ unpinned — a distinct
                // glyph from scroll-lock's 📌 badge so two pins never collide.
                // Inserted after the color swatch (below) for order 🎨 ▲ _ ×.
                notePinBtn = mkNoteBtn('▲', 'always on top');
                notePinBtn.classList.add('btn-pin');   // styling + test hook
                titleBar.insertBefore(noteFontDownBtn, minBtn);
                titleBar.insertBefore(noteFontUpBtn, minBtn);
                titleBar.insertBefore(noteWrapBtn, minBtn);
            }

            // Editor-only: show line numbers (default true). Notes ignore it.
            const lineNums = !isNote && (docs ? !!initialDoc.lineNums
                : (appData.lineNums !== undefined ? !!appData.lineNums : true));

            const textarea = document.createElement('textarea');
            textarea.className = 'app-textarea' + (wrap ? ' wrap' : '');
            textarea.value = docs ? (initialDoc.content || '')
                : (appData.content || '');
            textarea.spellcheck = isNote;
            if (isNote) textarea.placeholder = 'Note…';

            // Editor-only toolbar (New / Open / Save / Save As / Wrap / # line
            // numbers + a right-aligned filename + dirty dot). Built here; wired
            // below once the window object exists.
            let toolbar = null, newBtn, openBtn, saveBtn, saveAsBtn, wrapBtn,
                lineNumBtn, fileNameEl, dirtyEl, hostBtn = null,
                folderBtn = null;
            if (!isNote) {
                toolbar = document.createElement('div');
                toolbar.className = 'app-toolbar';
                const mkBtn = (label, ttl) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.textContent = label;
                    if (ttl) b.title = ttl;
                    return b;
                };
                newBtn = mkBtn('New', 'new empty file');
                openBtn = mkBtn('Open', 'open a file from the server');
                saveBtn = mkBtn('Save', 'save to the server');
                saveAsBtn = mkBtn('Save As', 'save to a new path');
                wrapBtn = mkBtn('Wrap', 'toggle word wrap');
                lineNumBtn = mkBtn('#', 'toggle line numbers');
                // Host picker (#46): which broker this editor's Open/Save dial.
                // Generic single-doc editors only — an agent-docs (tabbed)
                // window is bound to one folder's docs on one host, so a
                // window-level host switch makes no sense there (it would have
                // to detach every tab). `docs` gates that exactly as New/Open/
                // Save As are hidden for tabbed windows just below.
                if (!docs) {
                    hostBtn = mkBtn('', 'switch which host this editor '
                        + 'reads and writes');
                    hostBtn.className = 'app-host-btn';
                }
                // Re-home / choose-folder (#46 follow-up): point THIS window at a
                // chosen folder (file tools only — does NOT move the terminal or
                // the agent). On an agent-docs (tabbed) window it re-reads
                // AGENTS.md/CLAUDE.md from the new folder; on a generic editor it
                // re-roots where Open/Save browse. Present for both.
                folderBtn = mkBtn('', docs
                    ? 'edit a different folder’s AGENTS.md / CLAUDE.md'
                    : 'choose the folder this editor opens and saves in');
                folderBtn.className = 'app-host-btn';
                fileNameEl = document.createElement('span');
                fileNameEl.className = 'app-file-name';
                dirtyEl = document.createElement('span');
                dirtyEl.className = 'app-dirty';
                toolbar.appendChild(newBtn);
                toolbar.appendChild(openBtn);
                toolbar.appendChild(saveBtn);
                toolbar.appendChild(saveAsBtn);
                toolbar.appendChild(wrapBtn);
                toolbar.appendChild(lineNumBtn);
                if (hostBtn) toolbar.appendChild(hostBtn);
                if (folderBtn) toolbar.appendChild(folderBtn);
                toolbar.appendChild(fileNameEl);
                toolbar.appendChild(dirtyEl);
                // Agent-docs windows edit a FIXED pair of files (AGENTS.md +
                // CLAUDE.md) chosen by their tabs, so New/Open/Save As (which
                // would retarget the buffer) don't apply — keep Save/Wrap/#.
                if (docs) {
                    newBtn.style.display = 'none';
                    openBtn.style.display = 'none';
                    saveAsBtn.style.display = 'none';
                }
            }

            // Agent-docs tab strip ([AGENTS.md][CLAUDE.md][⚙ Sections]). Built
            // here; populated + wired by win._refreshTabBar once `win` exists.
            let tabBar = null;
            if (docs) {
                tabBar = document.createElement('div');
                tabBar.className = 'app-tabs';
            }

            // Editor-only: find bar (hidden until Ctrl+F) + a line-number gutter
            // beside the textarea. The gutter aligns row-for-row with the text
            // ONLY when word-wrap is off (a wrapped logical line spans several
            // visual rows but is still one number) — perfect wrapped alignment
            // is out of scope, documented here. bodyWrap is the flex row that
            // holds the gutter + textarea; win.body stays the textarea.
            let findBar = null, findInput, findNext, findPrev, findCount,
                findClose, gutter = null, bodyWrap = null;
            if (!isNote) {
                findBar = document.createElement('div');
                findBar.className = 'app-find';
                findInput = document.createElement('input');
                findInput.type = 'text';
                findInput.placeholder = 'Find';
                findInput.className = 'app-find-input';
                findPrev = document.createElement('button');
                findPrev.type = 'button';
                findPrev.textContent = '↑';
                findPrev.title = 'previous match';
                findNext = document.createElement('button');
                findNext.type = 'button';
                findNext.textContent = '↓';
                findNext.title = 'next match';
                findCount = document.createElement('span');
                findCount.className = 'app-find-count';
                findCount.textContent = '0/0';
                findClose = document.createElement('button');
                findClose.type = 'button';
                findClose.textContent = '×';
                findClose.title = 'close find';
                findBar.appendChild(findInput);
                findBar.appendChild(findPrev);
                findBar.appendChild(findNext);
                findBar.appendChild(findCount);
                findBar.appendChild(findClose);

                gutter = document.createElement('div');
                gutter.className = 'app-linenums';
                if (!lineNums) gutter.style.display = 'none';

                bodyWrap = document.createElement('div');
                bodyWrap.className = 'app-editor-body';
                bodyWrap.appendChild(gutter);
                bodyWrap.appendChild(textarea);
            }

            // Agent-docs windows: the Sections CRUD panel shown in place of the
            // editor body on the ⚙ Sections tab. Populated by _renderSectionsPanel.
            let sectionsPanel = null;
            if (docs) {
                sectionsPanel = document.createElement('div');
                sectionsPanel.className = 'app-sections-panel';
                sectionsPanel.style.display = 'none';
            }

            // titleBar is already the first child (buildAppChrome appended it).
            if (tabBar) dom.appendChild(tabBar);
            if (toolbar) dom.appendChild(toolbar);
            if (findBar) dom.appendChild(findBar);
            // Editors mount the flex bodyWrap (gutter + textarea); notes mount
            // the bare textarea exactly as before.
            if (bodyWrap) dom.appendChild(bodyWrap);
            else dom.appendChild(textarea);
            if (sectionsPanel) dom.appendChild(sectionsPanel);
            // Open straight onto the Sections tab (restored windows persist the
            // active tab): hide the editor chrome + body, show the panel. The
            // _renderSectionsPanel call happens after `win` is built, below.
            if (sectionsActiveOnOpen) {
                if (toolbar) toolbar.style.display = 'none';
                if (findBar) findBar.classList.remove('open');
                if (bodyWrap) bodyWrap.style.display = 'none';
                sectionsPanel.style.display = '';
            }
            addResizeHandles(dom);   // last children: edge/corner hit zones on top

            document.getElementById('desktop').appendChild(dom);
            document.getElementById('desktop').classList.remove('empty');

            const win = {
                id, sid, hostId: 'app',
                type: 'app', appKind,
                dom, body: textarea, titleText,
                // terminal fields explicitly absent
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
                // Always-on-top (#95), note-only. MUST be set in this literal so
                // win.pinned exists before the builder's terminal bringToFront(id)
                // computes the note's initial z-tier (floatZIndex reads it).
                pinned,
                // Multi-tab agent-docs model (null for notes + single-doc
                // editors): the per-tab file buffers + Sections tab, the active
                // tab index, and the window's cwd. win.filePath/dirty/wrap/
                // lineNums/agentsMdCwd below are kept as MIRRORS of the active
                // file doc so every legacy editor closure keeps working unchanged.
                tabs,
                activeTab,
                docsCwd: docs ? String(appData.agentsMdCwd || '') : null,
                // editor document state (notes ignore filePath/wrap/lineNums)
                filePath: docs ? (initialDoc.filePath || null)
                    : (appData.filePath != null ? String(appData.filePath) : null),
                // Source text encoding (#97), a MIRROR of the active file doc
                // for multi-tab windows (refreshDocGlobalsFrom keeps it in sync
                // on tab switch). Round-trips open -> edit -> save so a UTF-16/
                // cp1252 file is rewritten in its own encoding. Default utf-8.
                encoding: docs ? (initialDoc.encoding || 'utf-8')
                    : (appData.encoding || 'utf-8'),
                // Mirror the active doc's dirty for a restored multi-tab window.
                dirty: docs ? !!initialDoc.dirty : false,
                wrap,
                lineNums,
                // Per-note text size (#19); undefined (→ omitted on save) for
                // editors so it never pollutes their records.
                fontSize: isNote ? noteFontSize : undefined,
                // AGENTS.md editors: the folder whose AGENTS.md this edits. Set
                // from appData so it survives reload (saveAppWindow persists it),
                // and drives the CLAUDE.md save hook + template checklist below.
                // For a multi-tab window it MIRRORS the active doc — non-null only
                // while the AGENTS.md tab is active (so the CLAUDE save never runs
                // the hook). refreshDocGlobalsFrom keeps it in sync on tab switch.
                agentsMdCwd: docs
                    ? (initialDoc.isAgents ? String(appData.agentsMdCwd || '') : null)
                    : (appData.agentsMdCwd != null
                        ? String(appData.agentsMdCwd) : null),
                // Which broker this editor's /file/* ops dial. Defaults to
                // 'local' (generic editors + restored docs stay local); an
                // AGENTS.md editor opened from a remote terminal carries that
                // terminal's hostId so reads/writes hit the remote broker. A
                // removed host falls back to local via fileHost() below.
                fileHostId: appData.fileHostId || 'local',
                // Initial dir for a NEW editor's Open/Save dialogs: the
                // configured Control Panel start path when set (#73), else the
                // active terminal's cwd (#35) — so a blank editor saves at the
                // configured start path / "where I am" instead of the broker's
                // default dir. Seeded by launchTextEditor; ignored once filePath set.
                startDir: appData.startDir ? String(appData.startDir) : '',
                _saveTimer: null,
                // CodeMirror handle (null until/unless CM mounts for an editor).
                cmView: null,
            };
            windows.set(id, win);

            // ---- content accessor layer ----------------------------------
            // The rest of the code reads/writes the editor body through these,
            // so it never has to know whether a <textarea> or a CodeMirror view
            // is mounted. They start as textarea proxies (the fallback) and are
            // re-pointed at CM by mountCodeMirror() on a successful lazy load.
            // Notes also get them (harmless textarea proxies) so saveAppWindow
            // can read content uniformly.
            win.getContent = () => textarea.value;
            win.setContent = (str) => { textarea.value = str == null ? '' : String(str); };
            win.focusEditor = () => { try { textarea.focus(); } catch (_) {} };

            // ---- sticky-note text size + word-wrap controls (#19) -----------
            // Apply + persist per-note fontSize/wrap. The title-bar buttons were
            // built above; wire them now that `win` exists.
            if (isNote) {
                const applyNoteFont = () => {
                    textarea.style.fontSize = win.fontSize + 'px';
                };
                const applyNoteWrap = () => {
                    textarea.classList.toggle('wrap', !!win.wrap);
                    noteWrapBtn.classList.toggle('on', !!win.wrap);
                    noteWrapBtn.title = win.wrap
                        ? 'word wrap: on (click to turn off)'
                        : 'word wrap: off (click to turn on)';
                };
                // Always-on-top toggle (#95), mirroring applyNoteWrap: reflect
                // win.pinned onto the ▲/△ button (accent + glyph + a11y).
                const applyPin = () => {
                    const on = win.pinned !== false;
                    notePinBtn.classList.toggle('on', on);   // accent when pinned
                    notePinBtn.textContent = on ? '▲' : '△'; // ▲ pinned / △ unpinned
                    notePinBtn.title = on
                        ? 'always on top: on (click to unpin)'
                        : 'always on top: off (click to pin)';
                    notePinBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
                };
                applyNoteFont();
                applyNoteWrap();
                applyPin();
                noteFontDownBtn.addEventListener('click', () => {
                    win.fontSize = Math.max(NOTE_FONT_MIN, win.fontSize - 1);
                    applyNoteFont();
                    saveAppWindow(win);
                });
                noteFontUpBtn.addEventListener('click', () => {
                    win.fontSize = Math.min(NOTE_FONT_MAX, win.fontSize + 1);
                    applyNoteFont();
                    saveAppWindow(win);
                });
                noteWrapBtn.addEventListener('click', () => {
                    win.wrap = !win.wrap;
                    applyNoteWrap();
                    saveAppWindow(win);
                });
                notePinBtn.addEventListener('click', () => {
                    win.pinned = !win.pinned;   // literal guarantees a clean boolean
                    applyPin();
                    saveAppWindow(win);         // persist the new choice
                    // Re-apply the note's z with the NEW pinned value via the
                    // canonical raise path: floatZIndex now routes an unpinned
                    // note to the normal tier (and a re-pinned one back to
                    // NOTE_Z_BASE). The button's own mousedown stopPropagation
                    // (below) suppresses the dom-mousedown raise, so without this
                    // the live z-index would not change to reflect the toggle.
                    bringToFront(win.id);
                });
                // A title-bar mousedown otherwise starts a window drag (wireDrag
                // listens on the title bar; tiled notes drag immediately) — stop
                // it on these buttons, like min/close do.
                for (const b of [noteFontDownBtn, noteFontUpBtn, noteWrapBtn,
                                 notePinBtn]) {
                    b.addEventListener('mousedown', (e) => e.stopPropagation());
                }
            }

            // ---- multi-tab doc model (agent-docs windows only) -------------
            // The editor surface (textarea OR the mounted CM view) always shows
            // ONE "bound file doc". These helpers snapshot the bound doc, point
            // the legacy win.* mirrors at a doc, and switch which doc is bound.
            // For single-doc editors + notes win.tabs is null and they are no-ops
            // returning null, so the legacy paths are untouched.
            const agentsFileDoc = () => (win.tabs
                ? (win.tabs.find(d => d.kind === 'file' && d.isAgents) || null)
                : null);
            // The file doc the editor surface currently HOLDS. CRITICAL: on the
            // Sections tab the editor isn't rebound (it's just hidden), so the
            // bound doc stays whatever file tab was last shown — NOT necessarily
            // the AGENTS doc. Capturing into the active TAB instead of the bound
            // doc would write the wrong tab's text into a doc, so this is the
            // single source of truth for "what's in the editor right now".
            win._boundDoc = win.tabs ? (initialDoc || null) : null;
            const activeFileDoc = () => (win.tabs ? (win._boundDoc || null) : null);
            // Point the legacy globals at `doc` so every closure that reads
            // win.filePath/dirty/wrap/lineNums/agentsMdCwd sees the active doc.
            const refreshDocGlobalsFrom = (doc) => {
                if (!doc) return;
                win.filePath = doc.filePath || null;
                win.dirty = !!doc.dirty;
                win.wrap = !!doc.wrap;
                win.lineNums = !!doc.lineNums;
                win.encoding = doc.encoding || 'utf-8';
                win.agentsMdCwd = doc.isAgents ? (win.docsCwd || null) : null;
            };
            win._activeFileDoc = activeFileDoc;
            // Snapshot the live editor (content + CM state + scroll) back into
            // the bound doc. Called before any tab switch and before persisting
            // (saveAppWindow), so a doc is never serialized stale.
            win._captureActiveDoc = () => {
                if (!win.tabs) return;
                const d = activeFileDoc();
                if (!d) return;
                if (win.cmView) {
                    try {
                        d.content = win.cmView.state.doc.toString();
                        d.cmState = win.cmView.state;
                        d.scrollTop = win.cmView.scrollDOM.scrollTop;
                        d.scrollLeft = win.cmView.scrollDOM.scrollLeft;
                    } catch (_) {}
                } else {
                    d.content = textarea.value;
                    d.scrollTop = textarea.scrollTop;
                    d.scrollLeft = textarea.scrollLeft;
                }
                d.dirty = win.dirty;
                // Keep the bound doc's encoding mirror (#97) in sync with the
                // window-level mirror (the UTF-8 fallback flips win.encoding).
                d.encoding = win.encoding || 'utf-8';
            };
            // Rebuild the tab strip (labels + per-doc dirty dots + active state).
            win._refreshTabBar = () => {
                if (!tabBar) return;
                tabBar.textContent = '';
                win.tabs.forEach((d, i) => {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'app-tab' + (i === win.activeTab ? ' active' : '');
                    if (d.kind === 'sections') {
                        b.textContent = '⚙ Sections';
                    } else {
                        b.textContent = d.name;
                        const dot = document.createElement('span');
                        dot.className = 'app-tab-dot';
                        dot.textContent = d.dirty ? '●' : '';
                        b.appendChild(dot);
                    }
                    const onClick = (e) => { e.stopPropagation(); win._switchDocTab(i); };
                    b.addEventListener('click', onClick);
                    b.addEventListener('mousedown', stopProp);
                    tabBar.appendChild(b);
                });
            };
            // Switch the visible tab. Captures the outgoing doc, then either
            // re-binds the editor to the target file doc (restoring its per-tab
            // CM state/scroll) or shows the Sections panel.
            win._switchDocTab = (idx) => {
                if (!win.tabs || idx < 0 || idx >= win.tabs.length) return;
                win._captureActiveDoc();
                win.activeTab = idx;
                const doc = win.tabs[idx];
                if (doc.kind === 'sections') {
                    if (toolbar) toolbar.style.display = 'none';
                    if (findBar) findBar.classList.remove('open');
                    if (win._tplPanel) win._tplPanel.style.display = 'none';
                    if (bodyWrap) bodyWrap.style.display = 'none';
                    if (sectionsPanel) sectionsPanel.style.display = '';
                    if (win._renderSectionsPanel) win._renderSectionsPanel();
                } else {
                    if (sectionsPanel) sectionsPanel.style.display = 'none';
                    if (toolbar) toolbar.style.display = '';
                    if (bodyWrap) bodyWrap.style.display = '';
                    win._boundDoc = doc;        // editor now holds this doc
                    refreshDocGlobalsFrom(doc);
                    if (win.cmView) {
                        win._suppressCmChange = true;
                        try {
                            win.cmView.setState(
                                doc.cmState || win._makeEditorState(doc));
                        } finally { win._suppressCmChange = false; }
                        // Reconfigure language/wrap/line-num compartments to the
                        // target doc (its saved cmState already matches, but a
                        // freshly-built state needs them set from the mirrors).
                        if (win._setCmLanguage) win._setCmLanguage();
                        if (win._applyCmWrap) win._applyCmWrap();
                        if (win._applyCmLineNums) win._applyCmLineNums();
                        try {
                            win.cmView.scrollDOM.scrollTop = doc.scrollTop || 0;
                            win.cmView.scrollDOM.scrollLeft = doc.scrollLeft || 0;
                        } catch (_) {}
                        if (win._closeCmSearch) win._closeCmSearch();
                    } else {
                        textarea.value = doc.content || '';
                        textarea.classList.toggle('wrap', !!doc.wrap);
                        if (gutter) {
                            gutter.style.display = doc.lineNums ? '' : 'none';
                        }
                        if (win._renderLineNums) win._renderLineNums();
                        try {
                            textarea.scrollTop = doc.scrollTop || 0;
                            textarea.scrollLeft = doc.scrollLeft || 0;
                        } catch (_) {}
                        if (findBar) findBar.classList.remove('open');
                    }
                    if (win._updateFileUi) win._updateFileUi();
                    if (win._tplPanel) {
                        win._tplPanel.style.display = doc.isAgents ? '' : 'none';
                    }
                    if (doc.isAgents && win._renderTplPanel) win._renderTplPanel();
                    win.focusEditor();
                }
                if (win._refreshTabBar) win._refreshTabBar();
                saveAppWindow(win);
            };

            // Re-home (#46 follow-up): re-point a tabbed agent-docs window at a
            // NEW folder on the SAME host — re-read AGENTS.md + CLAUDE.md there
            // and replace the two file tabs IN PLACE, preserving the window, its
            // placement and tab group. UI-only: the terminal/agent is untouched.
            win._reHomeDocs = async (newCwd) => {
                if (!win.tabs) return;
                newCwd = String(newCwd || '');
                if (!newCwd || newCwd === win.docsCwd) return;
                // Don't silently discard unsaved edits in the file tabs.
                win._captureActiveDoc();
                const fileDocs = win.tabs.filter(d => d.kind === 'file');
                if (fileDocs.some(d => d.dirty)) {
                    const ok = await openConfirmDialog({
                        title: 'Re-home window',
                        message: 'Re-home this window to:\n' + newCwd
                            + '\n\nUnsaved changes in the AGENTS.md / CLAUDE.md '
                            + 'tabs will be discarded.',
                        okLabel: 'Re-home', danger: true });
                    if (!ok) return;
                    // The window can be torn down (or its tabs cleared) while the
                    // dialog is open — re-check before the re-home sequence.
                    if (win.disposed || !win.tabs) return;
                }
                const hid = win.fileHostId || 'local';
                const host = hostById(hid) || localHost();
                const agentsPath = joinNative(newCwd, 'AGENTS.md');
                const claudePath = joinNative(newCwd, 'CLAUDE.md');
                // Generation guard: if a SECOND re-home starts while these reads
                // are in flight, the later one wins — an out-of-order completion
                // of this (now stale) call must not clobber the newer folder.
                const seq = (win._reHomeSeq = (win._reHomeSeq || 0) + 1);
                const [aRes, cRes] = await Promise.all([
                    editorFile().read(agentsPath, { host: host.id }),
                    editorFile().read(claudePath, { host: host.id }),
                ]);
                if (win.disposed || seq !== win._reHomeSeq) return;
                if ((aRes && aRes.error === 'auth_required')
                    || (cRes && cRes.error === 'auth_required')) {
                    promptFileHostAuth(host); return;
                }
                // A real read error (not "doesn't exist yet") aborts, leaving the
                // window on its old folder.
                if (aRes && !aRes.ok && aRes.error !== 'not_found') {
                    showNotice('re-home failed: ' + ((aRes && aRes.error) || '?'));
                    return;
                }
                win.docsCwd = newCwd;
                for (const d of fileDocs) {
                    const res = d.isAgents ? aRes : cRes;
                    d.content = (res && res.ok) ? (res.content || '') : '';
                    d.filePath = d.isAgents ? agentsPath : claudePath;
                    d.dirty = false;
                    d.cmState = null;       // force a rebuild from the new content
                    d.scrollTop = 0; d.scrollLeft = 0;
                }
                // Rebuild the live editor from the now-updated bound doc WITHOUT
                // capturing first (a capture would write the stale editor text
                // back over the content we just replaced).
                const bd = win._boundDoc;
                if (bd && bd.kind === 'file') {
                    refreshDocGlobalsFrom(bd);
                    if (win.cmView) {
                        win._suppressCmChange = true;
                        try { win.cmView.setState(win._makeEditorState(bd)); }
                        finally { win._suppressCmChange = false; }
                        if (win._setCmLanguage) win._setCmLanguage();
                        if (win._applyCmWrap) win._applyCmWrap();
                        if (win._applyCmLineNums) win._applyCmLineNums();
                    } else if (textarea) {
                        textarea.value = bd.content || '';
                    }
                }
                const title = (hid === 'local')
                    ? ('Agent docs — ' + newCwd)
                    : ('Agent docs — ' + (host.label || hid) + ':' + newCwd);
                win.name = title;
                if (titleText) titleText.textContent = title;
                updateTaskbarLabel(id);
                if (win._refreshTabBar) win._refreshTabBar();
                if (win._updateFileUi) win._updateFileUi();
                const activeDoc = win.tabs[win.activeTab];
                if (activeDoc && activeDoc.kind === 'sections'
                    && win._renderSectionsPanel) win._renderSectionsPanel();
                if (win._renderTplPanel) win._renderTplPanel();
                if (win._updateFolderBtn) win._updateFolderBtn();
                saveAppWindow(win);
                showNotice('re-homed to ' + newCwd);
            };

            // stopProp is shared by the note/tab/find/color button handlers below
            // (the dom-mousedown raise + min/close are wired by wireAppChrome).
            const stopProp = (e) => e.stopPropagation();

            // Window-color control (issue 5): the shared swatch-dropdown. App
            // windows now use the SAME unified 11-color palette as terminals
            // (#29); a sticky note's paper/fg are DERIVED from the chosen accent
            // (noteSwatchFor), so any palette/custom/recent color works — not
            // just the five legacy presets. A pick persists via saveAppWindow.
            const colorBtn = attachColorPicker(
                win, titleBar, PALETTE.map((c) => ({ color: c })),
                (sw) => {
                    if (win.disposed) return;        // dialog finished post-close
                    const c = normalizeHex(sw.color);
                    win.color = c;
                    dom.style.setProperty('--accent', c);
                    dom.classList.toggle('dark-accent', isDarkAccent(c));
                    if (isNote) {
                        const nc = noteSwatchFor(c);  // legacy triple or derived
                        dom.style.setProperty('--note-paper', nc.paper);
                        dom.style.setProperty('--note-fg', nc.fg);
                    }
                    saveRecentColor(c);              // global MRU (#29)
                    saveAppWindow(win);
                    updateTaskbarColor(id);
                });
            titleBar.insertBefore(colorBtn, minBtn);
            // The always-on-top toggle (#95, built in the isNote block above) sits
            // right of the color swatch, so insert it AFTER colorBtn → 🎨 ▲ _ ×.
            if (notePinBtn) titleBar.insertBefore(notePinBtn, minBtn);

            // Inline rename (notes + editors): double-click the title text to
            // edit it in place. commitRename trims/caps the text, falls back to
            // the old name when blank, then syncs win.name + the synthetic
            // session title + the taskbar chip + the store. A `renaming` flag
            // makes commit idempotent — Enter blurs the element, and the blur
            // listener would otherwise commit a second time.
            let renaming = false;
            const commitRename = () => {
                if (!renaming) return;
                renaming = false;
                titleText.contentEditable = 'false';
                let next = (titleText.textContent || '').trim().slice(0, 80);
                if (!next) next = win.name;            // blank -> keep old name
                titleText.textContent = next;
                if (next !== win.name) {
                    win.name = next;
                    const ss = sessions.get(id);
                    if (ss) ss.title = win.name;
                    updateTaskbarLabel(id);
                    saveAppWindow(win);
                }
            };
            const onTitleDbl = (e) => {
                e.stopPropagation();
                if (renaming) return;
                renaming = true;
                titleText.contentEditable = 'true';
                titleText.focus();
                // Select all the title text so a fresh type replaces it.
                try {
                    const r = document.createRange();
                    r.selectNodeContents(titleText);
                    const sel = window.getSelection();
                    sel.removeAllRanges();
                    sel.addRange(r);
                } catch (_) {}
            };
            const onTitleKey = (e) => {
                if (!renaming) return;
                if (e.key === 'Enter') {
                    e.preventDefault();
                    titleText.blur();               // triggers commit via onBlur
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    renaming = false;
                    titleText.contentEditable = 'false';
                    titleText.textContent = win.name;   // revert
                    titleText.blur();
                }
            };
            // While editing, a title-bar mousedown must NOT start a window drag
            // (wireDrag listens on the title bar) — let the caret place itself.
            const onTitleEditDown = (e) => {
                if (titleText.isContentEditable) e.stopPropagation();
            };
            titleText.addEventListener('dblclick', onTitleDbl);
            titleText.addEventListener('keydown', onTitleKey);
            titleText.addEventListener('blur', commitRename);
            titleText.addEventListener('mousedown', onTitleEditDown);
            win.cleanups.push(() => {
                titleText.removeEventListener('dblclick', onTitleDbl);
                titleText.removeEventListener('keydown', onTitleKey);
                titleText.removeEventListener('blur', commitRename);
                titleText.removeEventListener('mousedown', onTitleEditDown);
            });

            // Autosave: debounced on input, immediate on blur so a click-away
            // or tab-close never loses the last edit. The buffer always lands
            // in appStore, so an unsaved editor buffer survives reload even
            // before it's written to a server file. Factored onto win so BOTH
            // the textarea 'input' listener AND CodeMirror's updateListener run
            // the same dirty/lineNums/autosave path (no duplicated logic).
            const onInput = () => {
                // Editor only: mark dirty (unsaved vs the server file) + show
                // the dot. The note has no file, so "dirty" is meaningless.
                // For a multi-tab window the dirty flag lives on the active doc;
                // win.dirty is its mirror and the per-tab dot tracks it too.
                if (!isNote && !win.dirty) {
                    win.dirty = true;
                    const d = activeFileDoc();
                    if (d) d.dirty = true;
                    if (win._updateFileUi) win._updateFileUi();
                    if (win._refreshTabBar) win._refreshTabBar();
                }
                if (win._renderLineNums) win._renderLineNums();
                clearTimeout(win._saveTimer);
                win._saveTimer = setTimeout(() => saveAppWindow(win), 400);
            };
            win._markEdited = onInput;
            const onBlur = () => { clearTimeout(win._saveTimer); saveAppWindow(win); };
            textarea.addEventListener('input', onInput);
            textarea.addEventListener('blur', onBlur);
            win.cleanups.push(() => {
                clearTimeout(win._saveTimer);
                textarea.removeEventListener('input', onInput);
                textarea.removeEventListener('blur', onBlur);
            });

            // ---- editor line numbers + find bar (editors only) ----
            // Line numbers: re-render only when the line count changes (cheap
            // for large files), and keep the gutter scroll-synced to the
            // textarea so the numbers track as the user scrolls. The gutter is
            // styled with the SAME font-size/line-height/padding-top as the
            // textarea so row N lines up with line N — exact only when wrap is
            // off (a wrapped logical line is one number but several rows).
            if (gutter) {
                let lastLineCount = -1;
                const renderLineNums = () => {
                    if (gutter.style.display === 'none') return;
                    const n = textarea.value.split('\n').length;
                    if (n === lastLineCount) return;
                    lastLineCount = n;
                    let s = '';
                    for (let i = 1; i <= n; i++) s += (i > 1 ? '\n' : '') + i;
                    gutter.textContent = s;
                };
                win._renderLineNums = () => { lastLineCount = -1; renderLineNums(); };
                const onScroll = () => { gutter.scrollTop = textarea.scrollTop; };
                textarea.addEventListener('scroll', onScroll);
                win.cleanups.push(() =>
                    textarea.removeEventListener('scroll', onScroll));
                win._renderLineNums();
            }

            if (findBar) {
                // Case-insensitive find over the textarea value. matches holds
                // {start,end} ranges recomputed on every input (so edits never
                // leave a stale index); cur points at the active one.
                let matches = [], cur = -1;
                const recompute = () => {
                    matches = [];
                    const needle = findInput.value;
                    if (needle) {
                        const hay = textarea.value.toLowerCase();
                        const nlc = needle.toLowerCase();
                        let i = hay.indexOf(nlc);
                        while (i !== -1) {
                            matches.push({ start: i, end: i + nlc.length });
                            i = hay.indexOf(nlc, i + Math.max(1, nlc.length));
                        }
                    }
                    if (cur >= matches.length) cur = matches.length - 1;
                };
                const updateCount = () => {
                    findCount.textContent = matches.length
                        ? ((cur + 1) + '/' + matches.length) : '0/0';
                };
                // keepFocus leaves the caret in the find input (incremental
                // type-ahead); next/prev/Enter pass false to focus the textarea
                // on the landed match, as the brief specifies.
                const select = (idx, keepFocus) => {
                    if (!matches.length) { updateCount(); return; }
                    cur = (idx + matches.length) % matches.length;
                    const m = matches[cur];
                    if (!keepFocus) textarea.focus();
                    textarea.setSelectionRange(m.start, m.end);
                    // Pull the selection into view (textarea has no native
                    // scrollIntoView for a range): approximate by line.
                    const line = textarea.value.slice(0, m.start).split('\n').length;
                    const lh = parseFloat(getComputedStyle(textarea).lineHeight) || 18;
                    textarea.scrollTop = Math.max(0, (line - 1) * lh
                        - textarea.clientHeight / 2);
                    if (gutter) gutter.scrollTop = textarea.scrollTop;
                    updateCount();
                };
                const openFind = () => {
                    findBar.classList.add('open');
                    recompute();
                    findInput.focus();
                    findInput.select();
                    updateCount();
                };
                const closeFind = () => {
                    findBar.classList.remove('open');
                    textarea.focus();
                };
                win._openFind = openFind;
                const onFindInput = () => {
                    recompute();
                    // Jump to the first match (or the retained one) as the user
                    // types, keeping the caret in the find box; no matches -> 0/0.
                    if (matches.length) select(Math.max(0, cur), true);
                    else { cur = -1; updateCount(); }
                };
                const onFindKey = (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        recompute();
                        select(cur + (e.shiftKey ? -1 : 1));
                    } else if (e.key === 'Escape') {
                        e.preventDefault();
                        closeFind();
                    }
                };
                const onNext = (e) => { e.stopPropagation(); recompute(); select(cur + 1); };
                const onPrev = (e) => { e.stopPropagation(); recompute(); select(cur - 1); };
                const onCloseFind = (e) => { e.stopPropagation(); closeFind(); };
                findInput.addEventListener('input', onFindInput);
                findInput.addEventListener('keydown', onFindKey);
                findInput.addEventListener('mousedown', stopProp);
                findNext.addEventListener('click', onNext);
                findNext.addEventListener('mousedown', stopProp);
                findPrev.addEventListener('click', onPrev);
                findPrev.addEventListener('mousedown', stopProp);
                findClose.addEventListener('click', onCloseFind);
                findClose.addEventListener('mousedown', stopProp);
                win.cleanups.push(() => {
                    findInput.removeEventListener('input', onFindInput);
                    findInput.removeEventListener('keydown', onFindKey);
                    findInput.removeEventListener('mousedown', stopProp);
                    findNext.removeEventListener('click', onNext);
                    findNext.removeEventListener('mousedown', stopProp);
                    findPrev.removeEventListener('click', onPrev);
                    findPrev.removeEventListener('mousedown', stopProp);
                    findClose.removeEventListener('click', onCloseFind);
                    findClose.removeEventListener('mousedown', stopProp);
                });
            }

            // Ctrl+S saves to the server file (falls back to Save As); Ctrl+F
            // opens the find bar. Editors only — notes have neither. Bound on
            // the textarea (its keydown is where editor typing focus lives).
            if (!isNote) {
                const onEditorKey = (e) => {
                    if (e.ctrlKey || e.metaKey) {
                        const k = e.key.toLowerCase();
                        if (k === 's') {
                            e.preventDefault();
                            if (win._saveToServer) win._saveToServer();
                        } else if (k === 'f') {
                            e.preventDefault();
                            if (win._openFind) win._openFind();
                        }
                    }
                };
                textarea.addEventListener('keydown', onEditorKey);
                win.cleanups.push(() =>
                    textarea.removeEventListener('keydown', onEditorKey));
            }

            // ---- editor toolbar wiring (server file Open/Save) ----
            if (toolbar) {
                const updateFileUi = () => {
                    fileNameEl.textContent = win.filePath || '(unsaved)';
                    fileNameEl.title = win.filePath || 'no file — Save writes a new one';
                    dirtyEl.textContent = win.dirty ? '●' : '';
                    wrapBtn.classList.toggle('on', !!win.wrap);
                    lineNumBtn.classList.toggle('on', !!win.lineNums);
                };
                win._updateFileUi = updateFileUi;
                // Resolve the broker this editor's file ops target. Re-read
                // win.fileHostId each call so a host removed mid-session is seen.
                // FAIL CLOSED: a known-remote host that no longer resolves
                // returns null (the op aborts with a notice) rather than
                // falling back to local — a remote absolute path can also exist
                // INSIDE the local editor_root, so a fallback could silently
                // overwrite a LOCAL file. Only the local editor (no/`'local'`
                // fileHostId) ever resolves to localHost(). (Codex review.)
                const fileHost = () => {
                    const h = hostById(win.fileHostId);
                    if (h) return h;
                    if (!win.fileHostId || win.fileHostId === 'local') {
                        return localHost();
                    }
                    return null;
                };
                // filePath is an ABSOLUTE host path now (#35), so split on EITHER
                // separator (C:\a\b.txt as well as /a/b.txt). dirOf('') and a new
                // editor's null filePath fall back to win.startDir (the configured
                // start path when set, else the terminal cwd the editor was opened
                // at) so Open/Save start at the configured start path / "where I am".
                const dirOf = (p) => {
                    if (!p) return win.startDir || '';
                    const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'));
                    return i === -1 ? (win.startDir || '') : p.slice(0, i);
                };
                const baseOf = (p) => baseName(p);
                // AGENTS.md guarantee: after a successful save of an AGENTS.md
                // editor, ensure <cwd>/CLAUDE.md exists and references it with a
                // bare `@AGENTS.md` line (Claude Code's include directive). Only
                // notices when it actually modified the file. Best-effort —
                // failures are swallowed so they never block the AGENTS.md save.
                const ensureClaudeMd = async (cwd, host) => {
                    // Caller threads the SAME host used for the AGENTS.md write
                    // so the read+write pair can't straddle two brokers if the
                    // host is edited/removed during an await gap (Codex review).
                    const h = host || fileHost();
                    if (!h) return;                       // host gone: best-effort
                    // C3 (no overwrite-behind): when this window has a CLAUDE.md
                    // tab, route the ensure through its in-memory BUFFER so the
                    // buffer stays == disk and a later CLAUDE save can't drop the
                    // @AGENTS.md line. Fall back to a disk read-modify-write only
                    // for single-doc AGENTS editors (no CLAUDE tab).
                    const claudeDoc = win.tabs && win.tabs.find(
                        d => d.kind === 'file' && d.name === 'CLAUDE.md');
                    if (claudeDoc) {
                        const live = (claudeDoc === activeFileDoc());
                        const text = live ? win.getContent()
                            : (claudeDoc.content || '');
                        if (/(^|\n)@AGENTS\.md(\n|$)/.test(text)) return;
                        const next = text
                            ? (text + (text.endsWith('\n') ? '' : '\n')
                               + '@AGENTS.md\n')
                            : '@AGENTS.md\n';
                        claudeDoc.content = next;
                        if (!live) claudeDoc.cmState = null;  // stale: rebuild
                        if (live) win.setContent(next);       // reflect into view
                        if (claudeDoc.dirty) {
                            // User has unsaved CLAUDE edits: insert into the
                            // buffer, leave it dirty, ask them to save (don't
                            // clobber their work on disk).
                            if (win._refreshTabBar) win._refreshTabBar();
                            saveAppWindow(win);
                            showNotice('added @AGENTS.md to the CLAUDE.md tab — '
                                + 'save it to persist');
                        } else {
                            // Clean buffer: write through so buffer == disk.
                            const w = await editorFile().write(
                                joinNative(cwd, 'CLAUDE.md'), next,
                                { host: h.id });
                            if (w && w.ok) {
                                // Only mark clean if the user didn't edit CLAUDE
                                // during the write (overwrite-behind, Codex review).
                                if (!claudeDoc.dirty) {
                                    if (live) win.dirty = false;
                                }
                                if (win._refreshTabBar) win._refreshTabBar();
                                saveAppWindow(win);
                                showNotice('ensured CLAUDE.md references '
                                    + '@AGENTS.md');
                            } else {
                                // Write failed but the buffer now carries
                                // @AGENTS.md that disk lacks: surface it as dirty.
                                claudeDoc.dirty = true;
                                if (live) win.dirty = true;
                                if (win._refreshTabBar) win._refreshTabBar();
                                saveAppWindow(win);
                            }
                        }
                        return;
                    }
                    const path = joinNative(cwd, 'CLAUDE.md');
                    const res = await editorFile().read(path, { host: h.id });
                    let text = '';
                    if (res && res.ok) text = res.content || '';
                    else if (!(res && res.error === 'not_found')) return; // real error
                    // Line-aware check so a stray substring never counts.
                    if (/(^|\n)@AGENTS\.md(\n|$)/.test(text)) return;
                    const next = text
                        ? (text + (text.endsWith('\n') ? '' : '\n') + '@AGENTS.md\n')
                        : '@AGENTS.md\n';
                    const w = await editorFile().write(path, next,
                        { host: h.id });
                    if (w && w.ok) showNotice('ensured CLAUDE.md references @AGENTS.md');
                };
                const writeTo = async (path, host) => {
                    // Resolve the broker once and reuse it for the AGENTS.md
                    // CLAUDE.md hook below (host param lets doSaveAs pass the
                    // broker it browsed, so dialog+write+hook all agree).
                    const h = host || fileHost();
                    if (!h) {
                        showNotice('save failed: host unavailable');
                        return false;
                    }
                    // C1 (capture-before-await): snapshot WHICH doc, its content,
                    // and whether it's the AGENTS doc BEFORE the write, so a tab
                    // switch mid-write can't corrupt another doc's path/dirty/UI.
                    const doc = activeFileDoc();          // null for single-doc
                    const content = win.getContent();
                    const isAgentsSave = doc ? doc.isAgents : !!win.agentsMdCwd;
                    // Capture the target broker: if the editor's host is SWITCHED
                    // mid-write (switchEditorHost), the bytes still land on `h`,
                    // but we must NOT re-attach this host-`h` path to the now-
                    // different window — that would point Save at a foreign-host
                    // path (#46 review). Compared after the await below.
                    const tgtHostId = h.id;
                    // #97: write in the doc's source encoding so a UTF-16/cp1252
                    // file round-trips. `enc` is the active doc's (multi-tab) or
                    // the window's mirror, captured before the await with content.
                    const enc = (doc ? doc.encoding : win.encoding) || 'utf-8';
                    let res = await editorFile().write(path, content,
                        { host: h.id, encoding: enc });
                    // The source encoding can't store newly-typed characters:
                    // offer a one-time re-save as UTF-8 (styled confirm — never a
                    // silent convert). Confirm -> retry as utf-8 + flip this
                    // doc/window's encoding mirror; decline -> abort, stay dirty.
                    let savedAsUtf8 = false;
                    if (res && !res.ok && res.error === 'encode_failed') {
                        const label = res.encoding || enc;
                        const ok = await openConfirmDialog({
                            title: 'Save as UTF-8?',
                            message: 'This file is ' + label + ' and can’t store '
                                + 'the new characters. Save as UTF-8 instead?',
                            okLabel: 'Save as UTF-8' });
                        if (!ok || win.disposed) return false;
                        res = await editorFile().write(path, content,
                            { host: h.id, encoding: 'utf-8' });
                        if (res && res.ok) {
                            savedAsUtf8 = true;
                            // Flip the CAPTURED doc's encoding (a stable ref, so a
                            // tab switch during the confirm can't redirect it).
                            // win.encoding (the active-doc mirror) is flipped only
                            // below, under the same disposed/host-drift guard that
                            // protects filePath/dirty.
                            if (doc) doc.encoding = 'utf-8';
                        }
                    }
                    if (!res || !res.ok) {
                        showNotice('save failed: ' + ((res && res.error) || '?'));
                        return false;
                    }
                    const savedPath = res.path || path;
                    // Host switched (or window closed) during the write: the save
                    // to `h` succeeded — report it, but leave the window's
                    // filePath/dirty/host alone (re-attaching would cross hosts).
                    if (win.disposed || win.fileHostId !== tgtHostId) {
                        showNotice(savedAsUtf8 ? 'saved as UTF-8'
                            : 'saved ' + savedPath);
                        return true;
                    }
                    if (doc) {
                        doc.filePath = savedPath;
                        // Don't clobber edits typed DURING the in-flight write:
                        // read the doc's current buffer (the live editor if it's
                        // still bound, else its captured content) and only mark it
                        // clean when it still equals what we persisted; otherwise
                        // keep the newer text and stay dirty (Codex review).
                        const bound = (doc === activeFileDoc());
                        const liveNow = bound ? win.getContent() : doc.content;
                        const clean = (liveNow === content);
                        doc.content = liveNow;
                        doc.dirty = !clean;
                        if (bound) {
                            win.filePath = savedPath;
                            win.dirty = !clean;
                            if (savedAsUtf8) win.encoding = 'utf-8';
                            updateFileUi();
                            if (clean && win._setCmLanguage) win._setCmLanguage();
                        }
                        if (win._refreshTabBar) win._refreshTabBar();
                    } else {
                        win.filePath = savedPath;
                        win.dirty = false;
                        if (savedAsUtf8) win.encoding = 'utf-8';
                        updateFileUi();
                        // Save As may have changed the extension -> re-detect lang.
                        if (win._setCmLanguage) win._setCmLanguage();
                    }
                    saveAppWindow(win);
                    showNotice(savedAsUtf8 ? 'saved as UTF-8'
                        : 'saved ' + savedPath);
                    if (isAgentsSave) {
                        try {
                            await ensureClaudeMd(win.docsCwd || win.agentsMdCwd, h);
                        } catch (_) {}
                    }
                    return true;
                };
                // Flush every dirty file doc to its server path (close prompt for
                // multi-tab windows, where more than one doc can be dirty). Runs
                // the AGENTS->CLAUDE hook once if an AGENTS save happened. Single-
                // doc editors fall back to the normal Save.
                win._saveAllDirty = async () => {
                    if (!win.tabs) {
                        return win._saveToServer ? win._saveToServer() : false;
                    }
                    win._captureActiveDoc();
                    const h = fileHost();
                    if (!h) {
                        showNotice('save failed: host unavailable');
                        return false;
                    }
                    let allOk = true, savedAgents = false;
                    for (const d of win.tabs) {
                        if (d.kind !== 'file' || !d.dirty) continue;
                        if (!d.filePath) { allOk = false; continue; }
                        // #97: per-doc source encoding; encode_failed prompts a
                        // per-tab UTF-8 fallback (named so the user knows which).
                        const enc = d.encoding || 'utf-8';
                        let res = await editorFile().write(
                            d.filePath, d.content, { host: h.id, encoding: enc });
                        if (res && !res.ok && res.error === 'encode_failed') {
                            const label = res.encoding || enc;
                            const ok = await openConfirmDialog({
                                title: 'Save as UTF-8?',
                                message: 'The ' + d.name + ' tab is ' + label
                                    + ' and can’t store the new characters. Save '
                                    + 'as UTF-8 instead?',
                                okLabel: 'Save as UTF-8' });
                            if (ok && !win.disposed) {
                                res = await editorFile().write(d.filePath,
                                    d.content, { host: h.id, encoding: 'utf-8' });
                                if (res && res.ok) d.encoding = 'utf-8';
                            }
                        }
                        if (res && res.ok) {
                            d.dirty = false;
                            d.filePath = res.path || d.filePath;
                            if (d.isAgents) savedAgents = true;
                        } else { allOk = false; }
                    }
                    refreshDocGlobalsFrom(activeFileDoc());
                    updateFileUi();
                    if (win._refreshTabBar) win._refreshTabBar();
                    saveAppWindow(win);
                    if (savedAgents) {
                        try { await ensureClaudeMd(win.docsCwd, h); } catch (_) {}
                    }
                    return allOk;
                };
                // An AGENTS.md editor that Opens or Save-As's a DIFFERENT file
                // is no longer maintaining that folder's AGENTS.md, so drop the
                // special behavior: stop the CLAUDE.md hook + hide the template
                // checklist. Cheap and keeps the scoping honest (Codex review).
                const clearAgentsMd = () => {
                    if (!win.agentsMdCwd) return;
                    win.agentsMdCwd = null;
                    if (win._tplPanel) win._tplPanel.style.display = 'none';
                    saveAppWindow(win);
                };
                const doSaveAs = async () => {
                    // Capture the broker once so the file dialog and the write
                    // can't straddle two hosts if settings change mid-dialog.
                    const h = fileHost();
                    if (!h) { showNotice('save failed: host unavailable'); return false; }
                    const picked = await openFileDialog({
                        mode: 'save', startDir: dirOf(win.filePath),
                        suggestName: baseOf(win.filePath), host: h });
                    if (!picked) return false;
                    // Save As re-targets the file; if it's not the original
                    // AGENTS.md path, this window stops being an AGENTS.md editor.
                    if (win.agentsMdCwd && picked !== win.filePath) clearAgentsMd();
                    return await writeTo(picked, h);
                };
                const doSave = async () => {
                    if (win.filePath) return await writeTo(win.filePath);
                    return await doSaveAs();
                };
                // Exposed for Ctrl+S and the close-save dialog (returns a
                // truthy result on a successful write).
                win._saveToServer = doSave;
                const doOpen = async () => {
                    if (win.dirty && !(await openConfirmDialog({
                            title: 'Discard changes?',
                            message: 'Discard unsaved changes?',
                            okLabel: 'Discard', danger: true }))) return;
                    if (win.disposed) return;
                    // One host for both the browse and the read (see doSaveAs).
                    const h = fileHost();
                    if (!h) { showNotice('open failed: host unavailable'); return; }
                    const picked = await openFileDialog({
                        mode: 'open', startDir: dirOf(win.filePath), host: h });
                    if (!picked) return;
                    const res = await editorFile().read(picked, { host: h.id });
                    if (!res || !res.ok) {
                        showNotice('open failed: ' + ((res && res.error) || '?'));
                        return;
                    }
                    win.setContent(res.content || '');
                    win.filePath = res.path || picked;
                    win.dirty = false;
                    // #97: adopt the opened file's source encoding so Save
                    // round-trips it (mirror onto the bound doc for multi-tab).
                    win.encoding = res.encoding || 'utf-8';
                    const _od = activeFileDoc();
                    if (_od) _od.encoding = win.encoding;
                    // Opening a different file drops the AGENTS.md special mode.
                    if (win.agentsMdCwd) clearAgentsMd();
                    updateFileUi();
                    if (win._renderLineNums) win._renderLineNums();
                    // New file path -> re-detect + reconfigure CM language.
                    if (win._setCmLanguage) win._setCmLanguage();
                    saveAppWindow(win);
                    showNotice('opened ' + win.filePath);
                };
                const doNew = async () => {
                    if (win.dirty && !(await openConfirmDialog({
                            title: 'Discard changes?',
                            message: 'Discard unsaved changes?',
                            okLabel: 'Discard', danger: true }))) return;
                    if (win.disposed) return;
                    win.setContent('');
                    win.filePath = null;
                    win.dirty = false;
                    win.encoding = 'utf-8';       // fresh buffer -> plain UTF-8
                    const _nd = activeFileDoc();
                    if (_nd) _nd.encoding = 'utf-8';
                    updateFileUi();
                    if (win._renderLineNums) win._renderLineNums();
                    // Cleared path -> plain (no language).
                    if (win._setCmLanguage) win._setCmLanguage();
                    saveAppWindow(win);
                    if (win.focusEditor) win.focusEditor();
                };
                const doWrap = () => {
                    win.wrap = !win.wrap;
                    // Per-tab preference: mirror onto the active doc (multi-tab).
                    const d = activeFileDoc();
                    if (d) d.wrap = win.wrap;
                    // CM path reconfigures the line-wrapping compartment; the
                    // fallback toggles the textarea .wrap class as before.
                    if (win._applyCmWrap) win._applyCmWrap();
                    else textarea.classList.toggle('wrap', win.wrap);
                    updateFileUi();
                    saveAppWindow(win);
                };
                const doLineNums = () => {
                    win.lineNums = !win.lineNums;
                    const d = activeFileDoc();
                    if (d) d.lineNums = win.lineNums;
                    // CM path reconfigures its line-number gutter compartment;
                    // the fallback shows/hides the custom .app-linenums gutter.
                    if (win._applyCmLineNums) win._applyCmLineNums();
                    else {
                        if (gutter) gutter.style.display = win.lineNums ? '' : 'none';
                        if (win.lineNums && win._renderLineNums) win._renderLineNums();
                    }
                    updateFileUi();
                    saveAppWindow(win);
                };
                const wireBtn = (btn, fn) => {
                    const onClick = (e) => { e.stopPropagation(); fn(); };
                    btn.addEventListener('mousedown', stopProp);
                    btn.addEventListener('click', onClick);
                    win.cleanups.push(() => {
                        btn.removeEventListener('mousedown', stopProp);
                        btn.removeEventListener('click', onClick);
                    });
                };
                wireBtn(newBtn, doNew);
                wireBtn(openBtn, doOpen);
                wireBtn(saveBtn, doSave);
                wireBtn(saveAsBtn, doSaveAs);
                wireBtn(wrapBtn, doWrap);
                wireBtn(lineNumBtn, doLineNums);

                // ---- host picker (#46) ----
                // Reflect the editor's current broker on the toolbar button.
                const updateHostBtn = () => {
                    if (!hostBtn) return;
                    // Be honest about a since-removed remote: fileHost() is
                    // fail-closed (null), so don't relabel it "this broker"
                    // (#46 review) — show it as removed, mirroring the FM.
                    const id = win.fileHostId;
                    const h = hostById(id)
                        || ((!id || id === 'local') ? localHost() : null);
                    const lbl = h ? hostPickerLabel(h) : (id + ' (removed)');
                    hostBtn.textContent = '🖥 ' + lbl + ' ▾';
                    hostBtn.title = 'reads/writes on: ' + lbl
                        + ' — click to switch host';
                };
                const switchEditorHost = async (host) => {
                    if (!host || host.id === win.fileHostId) return;
                    // Detach an OPEN file: its absolute path belongs to the OLD
                    // host, and silently re-pointing Save at a same-spelled path
                    // on the NEW host could clobber an unrelated file (the
                    // editor's fail-closed invariant, see fileHost()). Keep the
                    // BUFFER text as an untitled doc on the new host; the user
                    // re-Saves there. Blank editors just switch.
                    if (win.filePath) {
                        // Be honest about a since-removed old host (mirror
                        // updateHostBtn): don't relabel it "this broker" / local
                        // (#46 review).
                        const oid = win.fileHostId;
                        const oldH = hostById(oid)
                            || ((!oid || oid === 'local') ? localHost() : null);
                        const oldLbl = oldH ? hostPickerLabel(oldH)
                            : (oid + ' (removed)');
                        const ok = await openConfirmDialog({
                            title: 'Switch host',
                            message: 'Switch this editor to "'
                                + hostPickerLabel(host) + '"?\n\nIt will detach from '
                                + oldLbl + ':' + win.filePath
                                + '\nThe text stays here as an unsaved document; '
                                + 'Save will write to "' + hostPickerLabel(host)
                                + '".',
                            okLabel: 'Switch' });
                        if (!ok) return;
                        if (win.disposed) return;
                        win.filePath = null;
                        if (win.agentsMdCwd) clearAgentsMd();
                        win.dirty = !!win.getContent();
                        if (win._setCmLanguage) win._setCmLanguage();
                    }
                    win.fileHostId = host.id;
                    win.startDir = '';        // new host -> its default dir
                    updateHostBtn();
                    if (win._updateFolderBtn) win._updateFolderBtn();
                    updateFileUi();
                    saveAppWindow(win);
                    // The confirm dialog stole focus from the editor; restore it.
                    if (win.focusEditor) win.focusEditor();
                    // Re-prompt the new host's login if it isn't authed yet.
                    editorFile().list('', { host: host.id }).then(r => {
                        if (win.disposed || win.fileHostId !== host.id) return;
                        if (r && r.error === 'auth_required') {
                            promptFileHostAuth(host);
                        }
                    });
                };
                if (hostBtn) {
                    wireBtn(hostBtn, () => {
                        const r = hostBtn.getBoundingClientRect();
                        showHostPicker(win.fileHostId, r.left, r.bottom,
                                       switchEditorHost);
                    });
                    updateHostBtn();
                }
                // ---- folder picker / re-home (#46 follow-up) ----
                // A 📁 control to point this window at a chosen folder on its
                // host. Agent-docs windows re-read AGENTS.md/CLAUDE.md there;
                // generic editors re-root where Open/Save browse. UI-only.
                const folderBase = (p) => {
                    const s = String(p || '').replace(/[\\/]+$/, '');
                    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
                    return i >= 0 ? s.slice(i + 1) : s;
                };
                const updateFolderBtn = () => {
                    if (!folderBtn) return;
                    const cwd = win.docsCwd || win.startDir || '';
                    const lbl = cwd ? folderBase(cwd)
                        : (win.tabs ? '(folder)' : '(default)');
                    folderBtn.textContent = '📁 ' + lbl + ' ▾';
                    folderBtn.title = (win.tabs
                        ? 'editing AGENTS.md / CLAUDE.md in: '
                        : 'Open/Save browse: ')
                        + (cwd || '(host default)')
                        + ' — click to choose a folder';
                };
                win._updateFolderBtn = updateFolderBtn;
                const doChooseFolder = async () => {
                    const h = fileHost();
                    if (!h) {
                        showNotice('this window’s host was removed — '
                            + 'pick a host first');
                        return;
                    }
                    const start = win.docsCwd || win.startDir
                        || dirOf(win.filePath) || '';
                    const picked = await openFileDialog(
                        { mode: 'dir', host: h, startDir: start });
                    if (!picked || win.disposed) return;
                    if (win.tabs) {
                        await win._reHomeDocs(picked);
                    } else {
                        win.startDir = picked;
                        saveAppWindow(win);
                        updateFolderBtn();
                        showNotice('this editor now opens/saves in ' + picked);
                    }
                };
                if (folderBtn) {
                    wireBtn(folderBtn, doChooseFolder);
                    updateFolderBtn();
                }
                // An app window's hostId is 'app', so the auth-form healing loop
                // (which keys on win.hostId) never touches the editor. Expose a
                // hook the form calls after ANY host authenticates, so a switch
                // to a not-yet-authed host recovers without a manual retry.
                win._onHostAuth = (hid) => {
                    if (hid === win.fileHostId) updateHostBtn();
                };
                updateFileUi();

                // ---- AGENTS.md tab: section checklist ----
                // Below the toolbar, one checkbox per library section (now from
                // the synced getSections(), not a hardcoded array). Each ticked
                // section lives in AGENTS.md as a delimited block
                // (<!-- tpl:ID -->\n...body...\n<!-- /tpl:ID -->); ticking inserts
                // it at the end, unticking removes that exact block. Rebuilt by
                // win._renderTplPanel on tab switch, after section-library edits,
                // and on a remote /state adopt. Built for multi-tab agent-docs
                // windows (which always have an AGENTS doc) + legacy single-doc
                // AGENTS editors.
                const buildTpl = win.tabs ? !!agentsFileDoc() : !!win.agentsMdCwd;
                if (buildTpl) {
                    const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                    const tplOpen = (t) => '<!-- tpl:' + t.id + ' -->';
                    const tplBlock = (t) =>
                        tplOpen(t) + '\n' + t.body + '\n<!-- /tpl:' + t.id + ' -->';
                    const tplRe = (t) => new RegExp(
                        '\\n?' + escRe(tplOpen(t)) + '[\\s\\S]*?'
                        + escRe('<!-- /tpl:' + t.id + ' -->') + '\\n?', 'g');
                    const panel = document.createElement('div');
                    panel.className = 'app-tpl-panel';
                    toolbar.insertAdjacentElement('afterend', panel);
                    win._tplPanel = panel;
                    // The AGENTS buffer to read checked-state from: the live
                    // editor when the AGENTS doc is bound, else its stored
                    // content (CLAUDE tab active, or a remote-sync rebuild).
                    const agentsBuffer = () => {
                        const ad = agentsFileDoc();
                        if (!ad) return win.getContent();   // single-doc editor
                        if (ad === activeFileDoc()) return win.getContent();
                        return ad.content || '';
                    };
                    win._renderTplPanel = () => {
                        panel.textContent = '';
                        const lbl = document.createElement('span');
                        lbl.className = 'app-tpl-lbl';
                        lbl.textContent = 'sections:';
                        panel.appendChild(lbl);
                        const secs = getSections();
                        if (!secs.length) {
                            const none = document.createElement('span');
                            none.className = 'app-tpl-lbl';
                            none.textContent = '(none — add on ⚙ Sections)';
                            panel.appendChild(none);
                            return;
                        }
                        const cur0 = agentsBuffer();
                        secs.forEach((t, i) => {
                            const wrapLbl = document.createElement('label');
                            wrapLbl.className = 'app-tpl-item';
                            wrapLbl.title = t.label || t.id;
                            const cb = document.createElement('input');
                            cb.type = 'checkbox';
                            cb.checked = cur0.indexOf(tplOpen(t)) !== -1;
                            const txt = document.createElement('span');
                            txt.textContent = t.label || t.id;
                            wrapLbl.appendChild(cb);
                            wrapLbl.appendChild(txt);
                            panel.appendChild(wrapLbl);
                            // The checklist is only interactive on the AGENTS tab,
                            // where the editor is bound to the AGENTS doc — so the
                            // toggle safely reads/writes win.getContent/setContent.
                            const onToggle = (e) => {
                                e.stopPropagation();
                                const cur = win.getContent();
                                const present = cur.indexOf(tplOpen(t)) !== -1;
                                if (cb.checked && !present) {
                                    win.setContent(cur
                                        + (cur && !cur.endsWith('\n') ? '\n' : '')
                                        + tplBlock(t) + '\n');
                                    // Lock trigger: the block (<!-- tpl:ID -->) is
                                    // now in a file, so the id must stop tracking
                                    // the label or the block would orphan. Freeze
                                    // by INDEX (t.id may be stale) and pin to the
                                    // id just written, so the frozen id always
                                    // equals the inserted delimiter.
                                    const writtenId = t.id;
                                    const curSecs = getSections();
                                    if (curSecs[i] && curSecs[i].auto) {
                                        setSections(curSecs.map((s, j) =>
                                            j === i
                                                ? { ...s, id: writtenId,
                                                    auto: false }
                                                : s));
                                    }
                                } else if (!cb.checked && present) {
                                    win.setContent(cur.replace(tplRe(t), '\n'));
                                }
                                // Shared dirty/lineNums/autosave path (setContent
                                // suppresses CM's auto-dirty).
                                onInput();
                            };
                            const onDown = (e) => e.stopPropagation();
                            cb.addEventListener('change', onToggle);
                            cb.addEventListener('mousedown', onDown);
                        });
                    };
                    win._renderTplPanel();
                    // Multi-tab: the checklist shows only while the AGENTS tab is
                    // active. Hidden on open when another tab is the active one.
                    if (win.tabs) {
                        const at = win.tabs[win.activeTab];
                        panel.style.display =
                            (at && at.kind === 'file' && at.isAgents) ? '' : 'none';
                    }
                }

                // ---- ⚙ Sections tab: the section-library CRUD editor ----
                // Edits the global, /state-synced section library (getSections /
                // setSections). Commit-on-change for text (debounced; the
                // originating panel is NOT re-rendered so the caret survives);
                // add/delete/reorder/reset rebuild immediately. Editing a body
                // does NOT rewrite blocks already inserted into a file.
                if (sectionsPanel) {
                    let commitTimer = null;
                    const readRows = () => {
                        const out = [];
                        sectionsPanel.querySelectorAll('.app-sec-row')
                            .forEach((row) => {
                                out.push({
                                    id: row.dataset.secId || '',
                                    label: (row.querySelector('.app-sec-label')
                                        || {}).value || '',
                                    body: (row.querySelector('.app-sec-body')
                                        || {}).value || '',
                                    auto: row.dataset.secAuto === '1',
                                });
                            });
                        return out;
                    };
                    const commitText = () => {
                        // Persist + sync, but skip re-rendering THIS panel so the
                        // field the user is typing in keeps focus.
                        setSections(readRows(), { skipPanelFor: win });
                    };
                    const commitStructural = (arr) => {
                        // Reorder/add/delete/reset: full rebuild is fine (no field
                        // focused, or the rebuild is the intended effect).
                        setSections(arr);
                    };
                    win._renderSectionsPanel = () => {
                        sectionsPanel.textContent = '';
                        const bar = document.createElement('div');
                        bar.className = 'app-sec-bar';
                        const addBtn = document.createElement('button');
                        addBtn.type = 'button';
                        addBtn.textContent = '+ Add section';
                        const resetBtn = document.createElement('button');
                        resetBtn.type = 'button';
                        resetBtn.textContent = 'Reset to defaults';
                        resetBtn.title = 'replace the library with the built-in '
                            + 'sections';
                        bar.appendChild(addBtn);
                        bar.appendChild(resetBtn);
                        sectionsPanel.appendChild(bar);
                        const note = document.createElement('div');
                        note.className = 'app-sec-note';
                        note.textContent = 'Checkboxes on the AGENTS.md tab · edits '
                            + "don't touch blocks already in a file · shared & synced";
                        note.title = 'These sections appear as checkboxes on the '
                            + 'AGENTS.md tab. Editing a section here does NOT change '
                            + 'blocks already inserted into a file. The library is '
                            + 'shared and synced across your browsers.';
                        sectionsPanel.appendChild(note);
                        addBtn.addEventListener('mousedown', stopProp);
                        addBtn.addEventListener('click', (e) => {
                            e.stopPropagation();
                            const arr = readRows();
                            // auto:true → id tracks the label as you type (empty
                            // label so the id starts blank, fills in live). Locks
                            // the first time it's ticked into a file.
                            arr.push({ id: '', label: '',
                                       body: '## New section\n\n- \n',
                                       auto: true });
                            win._focusNewSection = true;
                            commitStructural(arr);   // re-renders with the new row
                        });
                        resetBtn.addEventListener('mousedown', stopProp);
                        resetBtn.addEventListener('click', async (e) => {
                            e.stopPropagation();
                            if (!(await openConfirmDialog({
                                    title: 'Reset sections',
                                    message: 'Replace the section library with the '
                                        + 'built-in defaults?',
                                    okLabel: 'Reset', danger: true }))) return;
                            if (win.disposed) return;
                            commitStructural(DEFAULT_SECTIONS.map(
                                t => ({ id: t.id, label: t.label, body: t.body })));
                        });
                        // Grow each body to fit its content (no inner scrollbar
                        // for short bodies), capped at the CSS max-height so a
                        // long body scrolls internally instead of ballooning the
                        // panel. Needs the row in the DOM for a valid scrollHeight.
                        // scrollHeight excludes the border, but box-sizing:border-box
                        // makes style.height INCLUDE it — so add the vertical border
                        // back or the content box ends up 2px short and shows a thin
                        // inner scrollbar anyway.
                        const autosize = (ta) => {
                            ta.style.height = 'auto';
                            const cs = getComputedStyle(ta);
                            const border = parseFloat(cs.borderTopWidth || 0)
                                + parseFloat(cs.borderBottomWidth || 0);
                            ta.style.height =
                                Math.min(160, ta.scrollHeight + border) + 'px';
                        };
                        const secs = getSections();
                        secs.forEach((t, i) => {
                            const row = document.createElement('div');
                            row.className = 'app-sec-row';
                            row.dataset.secId = t.id;
                            row.dataset.secAuto = t.auto ? '1' : '';
                            const head = document.createElement('div');
                            head.className = 'app-sec-head';
                            const labelInput = document.createElement('input');
                            labelInput.className = 'app-sec-label';
                            labelInput.type = 'text';
                            labelInput.value = t.label;
                            labelInput.placeholder = 'Section label';
                            const idTag = document.createElement('span');
                            idTag.className = 'app-sec-id';
                            if (t.auto) {
                                // id still tracks the label; show the live slug
                                // (or a placeholder while the label is empty).
                                idTag.classList.add('app-sec-id-auto');
                                idTag.textContent = t.label.trim() ? t.id : '…';
                                idTag.title =
                                    'auto — locks when first inserted into a file';
                            } else {
                                idTag.textContent = t.id;
                                idTag.title = 'block id (immutable)';
                            }
                            const upBtn = document.createElement('button');
                            upBtn.type = 'button';
                            upBtn.textContent = '↑';
                            upBtn.title = 'move up';
                            upBtn.disabled = (i === 0);
                            const downBtn = document.createElement('button');
                            downBtn.type = 'button';
                            downBtn.textContent = '↓';
                            downBtn.title = 'move down';
                            downBtn.disabled = (i === secs.length - 1);
                            const delBtn = document.createElement('button');
                            delBtn.type = 'button';
                            delBtn.textContent = '✕';
                            delBtn.title = 'delete section';
                            head.appendChild(labelInput);
                            head.appendChild(idTag);
                            head.appendChild(upBtn);
                            head.appendChild(downBtn);
                            head.appendChild(delBtn);
                            const bodyInput = document.createElement('textarea');
                            bodyInput.className = 'app-sec-body';
                            bodyInput.value = t.body;
                            bodyInput.placeholder = 'Markdown inserted into '
                                + 'AGENTS.md when ticked';
                            row.appendChild(head);
                            row.appendChild(bodyInput);
                            sectionsPanel.appendChild(row);
                            autosize(bodyInput);   // fit to content now it's in DOM
                            const onTextInput = () => {
                                clearTimeout(commitTimer);
                                commitTimer = setTimeout(commitText, 350);
                            };
                            labelInput.addEventListener('input', () => {
                                // For an auto row, update only the VISIBLE id span
                                // live (not dataset.secId) — normalizeSectionsArray
                                // is the source of truth for the stored id; this is
                                // best-effort display and self-corrects on the next
                                // full re-render (auto ids aren't in any file yet).
                                if (row.dataset.secAuto === '1') {
                                    const live = slugifySectionId(
                                        labelInput.value, new Set());
                                    idTag.textContent =
                                        labelInput.value.trim() ? live : '…';
                                }
                                onTextInput();
                            });
                            bodyInput.addEventListener('input', () => {
                                autosize(bodyInput);   // grow/shrink while typing
                                onTextInput();
                            });
                            labelInput.addEventListener('mousedown', stopProp);
                            bodyInput.addEventListener('mousedown', stopProp);
                            const moveBy = (delta) => {
                                clearTimeout(commitTimer);
                                const arr = readRows();
                                const j = i + delta;
                                if (j < 0 || j >= arr.length) return;
                                const tmp = arr[i]; arr[i] = arr[j]; arr[j] = tmp;
                                commitStructural(arr);
                            };
                            upBtn.addEventListener('mousedown', stopProp);
                            upBtn.addEventListener('click', (e) => {
                                e.stopPropagation(); moveBy(-1);
                            });
                            downBtn.addEventListener('mousedown', stopProp);
                            downBtn.addEventListener('click', (e) => {
                                e.stopPropagation(); moveBy(1);
                            });
                            delBtn.addEventListener('mousedown', stopProp);
                            delBtn.addEventListener('click', (e) => {
                                e.stopPropagation();
                                clearTimeout(commitTimer);
                                const arr = readRows();
                                arr.splice(i, 1);
                                commitStructural(arr);
                            });
                        });
                        // Focus the just-added row's label so the user can type
                        // its name immediately (set by the + Add section handler).
                        if (win._focusNewSection) {
                            win._focusNewSection = false;
                            const rows = sectionsPanel
                                .querySelectorAll('.app-sec-row');
                            const last = rows[rows.length - 1];
                            const li = last
                                && last.querySelector('.app-sec-label');
                            if (li) { li.focus(); li.select(); }
                        }
                    };
                }
            }

            // ---- CodeMirror 6 upgrade (editors only) ----------------------
            // Lazily import CM and, on success, mount an EditorView inside the
            // flex bodyWrap, replacing the <textarea> as the live editor. The
            // accessor layer is re-pointed at CM so every existing path (Open/
            // Save/Save As/New/autosave/dirty/Ctrl+S/AGENTS templates/persist)
            // keeps working unchanged. ANY failure (offline LAN broker, CDN
            // blocked) is caught and warned — the textarea editor stays exactly
            // as it was. Never throws out of openAppWindow. Notes never enter
            // here (no bodyWrap). The mount is fire-and-forget; the window is
            // already fully usable on the textarea while CM loads.
            if (!isNote && bodyWrap) {
                // Build + swap in the CM view. Kept as a closure so it captures
                // textarea/gutter/findBar/bodyWrap/onInput/win already in scope.
                const mountCodeMirror = (CM) => {
                    const { EditorView } = CM.view;
                    const { EditorState, Compartment } = CM.state;
                    const { keymap, lineNumbers, highlightActiveLine,
                            highlightActiveLineGutter, drawSelection,
                            rectangularSelection, crosshairCursor } = CM.view;
                    const { history, defaultKeymap, historyKeymap,
                            indentWithTab } = CM.commands;
                    const { syntaxHighlighting, defaultHighlightStyle,
                            bracketMatching, foldGutter, foldKeymap,
                            indentOnInput } = CM.language;
                    const { searchKeymap, highlightSelectionMatches,
                            openSearchPanel, closeSearchPanel } = CM.search;
                    const { oneDark } = CM.theme;
                    const closeBrackets =
                        CM.autocomplete && CM.autocomplete.closeBrackets;
                    const closeBracketsKeymap =
                        (CM.autocomplete && CM.autocomplete.closeBracketsKeymap) || [];

                    // Reconfigurable compartments: language (re-detected on path
                    // change), line-wrapping (Wrap button), line-number gutter
                    // (# button). All start from the window's saved prefs.
                    const langComp = new Compartment();
                    const wrapComp = new Compartment();
                    const lineNumComp = new Compartment();
                    win._cmLangComp = langComp;
                    win._cmWrapComp = wrapComp;
                    win._cmLineNumComp = lineNumComp;

                    // A theme that makes CM fill the flex body and sit on the
                    // app's dark editor surface; oneDark supplies the token
                    // colors. height:100% + min-height:0 so it scrolls inside
                    // bodyWrap rather than growing the window.
                    const fillTheme = EditorView.theme({
                        '&': { height: '100%', fontSize: '13px' },
                        '.cm-scroller': {
                            fontFamily: "Consolas, 'Liberation Mono', monospace",
                            lineHeight: '1.45',
                        },
                    });

                    // Per-doc state factory: a multi-tab window builds/restores a
                    // separate EditorState per file tab (isolated undo + selection)
                    // — so content/wrap/line-nums/language come from the passed
                    // doc. A single-doc editor passes null and reads the window
                    // globals exactly as before. makeEditorState is stored on win
                    // so switchDocTab can build a fresh state on first visit to a
                    // tab (doc.cmState || makeEditorState(doc)).
                    const docContentOf = (doc) =>
                        doc ? (doc.content || '') : win.getContent();
                    const docWrapOf = (doc) => doc ? !!doc.wrap : !!win.wrap;
                    const docLineNumsOf = (doc) =>
                        doc ? !!doc.lineNums : !!win.lineNums;
                    const langExtFor = (doc) => {
                        const fp = doc ? doc.filePath : win.filePath;
                        const key = detectLanguage(fp, docContentOf(doc));
                        const make = key && CM.langs[key];
                        if (!make) return [];
                        // A broken/undefined (legacy) parser must degrade to plain
                        // text for THIS file only, never throw out of makeEditorState
                        // and drop the whole editor to the textarea fallback (codex).
                        try { return make(); }
                        catch (e) {
                            console.warn('[webterm] language load failed:', key, e);
                            return [];
                        }
                    };
                    const makeEditorState = (doc) => EditorState.create({
                        doc: docContentOf(doc),
                        extensions: [
                            lineNumComp.of(docLineNumsOf(doc) ? lineNumbers() : []),
                            highlightActiveLineGutter(),
                            history(),
                            foldGutter ? foldGutter() : [],
                            drawSelection(),
                            indentOnInput(),
                            syntaxHighlighting(defaultHighlightStyle,
                                { fallback: true }),
                            bracketMatching(),
                            closeBrackets ? closeBrackets() : [],
                            rectangularSelection(),
                            crosshairCursor(),
                            highlightActiveLine(),
                            highlightSelectionMatches(),
                            wrapComp.of(docWrapOf(doc) ? EditorView.lineWrapping : []),
                            langComp.of(langExtFor(doc)),
                            // Ctrl+S FIRST so it wins over later keymaps; Ctrl+F
                            // (Mod-f) is provided by searchKeymap (openSearchPanel).
                            keymap.of([
                                { key: 'Mod-s', preventDefault: true,
                                  run: () => {
                                      if (win._saveToServer) win._saveToServer();
                                      return true;
                                  } },
                                ...closeBracketsKeymap,
                                ...defaultKeymap,
                                ...searchKeymap,
                                ...historyKeymap,
                                ...(foldKeymap || []),
                                indentWithTab,
                            ]),
                            oneDark,
                            fillTheme,
                            // Wire CM edits into the SAME autosave/dirty path the
                            // textarea uses. Programmatic setContent suppresses
                            // this via win._suppressCmChange.
                            EditorView.updateListener.of((u) => {
                                if (u.docChanged && !win._suppressCmChange) {
                                    onInput();
                                }
                            }),
                        ],
                    });
                    win._makeEditorState = makeEditorState;

                    // Mount against the active file doc (the AGENTS doc, hidden,
                    // when the Sections tab is the active one); single-doc editors
                    // pass null (reads the textarea content via win.getContent).
                    // Sync the active doc's buffer from the live textarea first so
                    // keystrokes typed during the CM load window aren't dropped
                    // (cmView is still null, so capture reads the textarea).
                    if (win.tabs && win._captureActiveDoc) win._captureActiveDoc();
                    const startDoc = win.tabs
                        ? (activeFileDoc() || agentsFileDoc()) : null;
                    const startState = makeEditorState(startDoc);
                    const view = new EditorView({ state: startState });

                    // Swap the DOM: hide the textarea + custom gutter, mount CM
                    // into the flex bodyWrap (which becomes win.body's child;
                    // win.body stays bodyWrap so getBoundingClientRect is valid).
                    textarea.style.display = 'none';
                    if (gutter) gutter.style.display = 'none';
                    if (findBar) findBar.classList.remove('open');
                    bodyWrap.appendChild(view.dom);
                    win.cmView = view;

                    // Re-point the accessor layer at CM.
                    win.getContent = () => view.state.doc.toString();
                    win.setContent = (str) => {
                        const next = str == null ? '' : String(str);
                        win._suppressCmChange = true;
                        try {
                            view.dispatch({
                                changes: { from: 0,
                                    to: view.state.doc.length, insert: next },
                            });
                        } finally {
                            win._suppressCmChange = false;
                        }
                    };
                    win.focusEditor = () => { try { view.focus(); } catch (_) {} };

                    // CM owns line numbers + search now: neutralize the custom
                    // gutter renderer + reroute Ctrl+F to CM's search panel so
                    // the old find bar (which closes over textarea.value) is
                    // never shown again.
                    win._renderLineNums = () => {};
                    win._openFind = () => { try { openSearchPanel(view); } catch (_) {} };
                    // Used by switchDocTab to close any open CM search panel so it
                    // doesn't carry over from one tab's buffer to the next.
                    win._closeCmSearch = () => {
                        try { closeSearchPanel(view); } catch (_) {}
                    };

                    // Compartment reconfigurers used by the toolbar buttons +
                    // the path-change language re-detect. They read the active
                    // doc's prefs (mirrored onto win.wrap/lineNums/filePath).
                    win._applyCmWrap = () => view.dispatch({ effects:
                        wrapComp.reconfigure(win.wrap ? EditorView.lineWrapping : []) });
                    win._applyCmLineNums = () => view.dispatch({ effects:
                        lineNumComp.reconfigure(win.lineNums ? lineNumbers() : []) });
                    win._setCmLanguage = () => view.dispatch({ effects:
                        langComp.reconfigure(langExtFor(activeFileDoc())) });

                    // Immediate flush on click-away (parity with the textarea's
                    // blur autosave): the hidden textarea's blur never fires once
                    // CM is mounted, so listen on CM's editable content DOM.
                    const onCmBlur = () => {
                        clearTimeout(win._saveTimer);
                        saveAppWindow(win);
                    };
                    view.contentDOM.addEventListener('blur', onCmBlur);
                    win.cleanups.push(() =>
                        view.contentDOM.removeEventListener('blur', onCmBlur));

                    // Tear the view down with the rest of the window.
                    win.cleanups.push(() => { try { view.destroy(); } catch (_) {} });

                    // If this window already has focus, hand it to CM — unless it
                    // opened straight onto the Sections tab (editor is hidden).
                    const sectionsActive = win.tabs && win.tabs[win.activeTab]
                        && win.tabs[win.activeTab].kind === 'sections';
                    if (frontId === win.id && !sectionsActive) win.focusEditor();
                };

                // Kick off the lazy load. Fire-and-forget: the window is fully
                // usable on the textarea while CM loads; on failure we keep it.
                loadCodeMirror().then((CM) => {
                    if (win.disposed) return;            // closed before load
                    if (win.cmView) return;              // already mounted
                    mountCodeMirror(CM);
                }).catch((e) => {
                    // MANDATORY fallback: keep the textarea editor as-is.
                    console.warn('[webterm] CodeMirror load failed; '
                        + 'using plain textarea editor', e);
                });
            }

            // Agent-docs windows: paint the tab strip now, and render the
            // Sections panel if the window opened straight onto that tab.
            if (win.tabs) {
                if (win._refreshTabBar) win._refreshTabBar();
                if (sectionsActiveOnOpen && win._renderSectionsPanel) {
                    win._renderSectionsPanel();
                }
            }

            // Raise / minimize / close / drag / 8-way resize / WM context menu
            // (the title-bar right-click menu — float<->tile, pin, minimize,
            // close, delete). × routes through requestCloseAppWindow so a dirty
            // editor offers to flush its server file first (closeWindow stays
            // prompt-free). The textarea keeps its native copy/paste menu.
            wireAppChrome(win, chrome, requestCloseAppWindow);

            // Manual taskbar item (app windows are never poll-managed). The
            // synthetic session entry keeps formatTitle / applyDisplaySettings
            // happy; updateTaskbarColor fixes the accent (buildTaskbarItem
            // colors from prefs, which app windows don't use).
            const appSess = { key: id, sid, id, title, stale: false,
                              kind: 'app', hostId: 'app' };
            sessions.set(id, appSess);
            // Sticky notes stay OFF the taskbar BY DEFAULT (todo2 task 9):
            // they default always-on-top (#95) so a chip is usually noise, and
            // an UNPINNED note that gets covered is still retrievable via the
            // desktop right-click Cascade/Tile menu (floatingWindowsOrdered
            // includes notes) — no lost-note trap. The synthetic session entry
            // above is kept (formatTitle / applyDisplaySettings rely on it);
            // only the chip is skipped HERE — the sticky mod's stickyTaskbar
            // toggle (#141) opts notes back in by adding the chip in its own
            // factory wrapper, riding this very session entry.
            if (!isNote) {
                const itemsHost = document.getElementById('taskbar-items');
                // Idempotent: reuse a chip already in the DOM rather than
                // appending a second one. Closing now removes the chip, so the
                // normal reopen path starts clean — but this still guards
                // against stray legacy chips or a double openAppWindow.
                if (!itemsHost.querySelector(
                        '.taskbar-item[data-session-id="' + cssEscape(id) + '"]')) {
                    itemsHost.appendChild(buildTaskbarItem(appSess));
                }
                updateTaskbarColor(id);
                updateTaskbarLabel(id);
                const emptyMsg = document.getElementById('taskbar-empty');
                if (emptyMsg) emptyMsg.remove();
            }

            saveAppWindow(win);
            // Restore tiling: a doc whose key is still in prefs._layout re-tiles
            // into its column (survives reload via the same path terminals use);
            // otherwise it stays floating with the geom applied above. New docs
            // are never in _layout, so they always start floating + pinned.
            if (findKeyInLayout(id)) placeWindowTiled(win);
            else bringToFront(id);
            return win;
        }

        async function launchTextEditor() {
            // Open the Open/Save dialogs at the Control Panel Default start path
            // when set (#73), else the active terminal's cwd+host (#35).
            const s = activeTerminalStart();
            let startDir = s.cwd;                 // fallback = today's behavior
            try {
                // Mirror fileHost(): hostById, then explicit 'local' fallback; a
                // removed remote stays null so we don't resolve a LOCAL startPath
                // for a remote-targeted editor (Codex review).
                let h = hostById(s.host);
                if (!h && (!s.host || s.host === 'local')) h = localHost();
                // #73: Control Panel Default start path wins when set; else the
                // active terminal cwd / broker default, exactly as before.
                if (h) startDir = (await resolveStartPath(h)) || s.cwd;
            } catch (_) { startDir = s.cwd; }
            openAppWindow({ id: newAppId('editor'), appKind: 'text-editor',
                            content: '', startDir: startDir, fileHostId: s.host });
        }

        // ---- mod registration: the text-editor window kind ----------------
        registerMod({
            id: 'editor',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['file', 'window'],   // #86: host filesystem read/write (ctx.file) + a window kind
            init: function (ctx) {
                // Route every editor /file/* op through the reviewed ctx.file
                // capability (#82); cleared on teardown so a disabled editor mod
                // falls back to the hoisted _modFileApi (see editorFile()).
                editorFile.cap = ctx.file;
                ctx.onUnload(function () { editorFile.cap = null; });
                // Register the text-editor kind (the #80 built-in spec, moved
                // here). serialize stays the shared core serializeAppWindow so
                // webterm:appwindows:v1 persistence is byte-identical; a duplicate
                // appKind throws -> initMod rolls the mod back; teardown removes
                // exactly THIS registration.
                ctx.registerWindowKind({
                    appKind: 'text-editor',
                    factory: function (d) { return openNoteOrEditorWindow(d); },
                    serialize: serializeAppWindow,
                    menu: {
                        label: 'Text editor',
                        iconKey: 'editor',   // #119: SVG pencil in the (+) menu
                        launch: function () { return launchTextEditor(); },
                    },
                });
            },
        });
