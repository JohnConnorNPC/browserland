        // ---- client-only "app" windows (sticky notes / text editor) -------
        // These are non-terminal windows (a <textarea>, no PTY/WebSocket).
        // Their content + geometry + color live in a SEPARATE localStorage
        // blob, never in `prefs` — the prefs GC (see refreshTaskbarInner)
        // would otherwise delete any key whose host segment ('app') isn't a
        // configured broker. Keyed by app id for O(1) upsert/delete.
        const APP_STORE_KEY = 'webterm:appwindows:v1';
        function loadAppStore() {
            try {
                const o = JSON.parse(localStorage.getItem(APP_STORE_KEY) || '{}');
                return (o && typeof o === 'object' && !Array.isArray(o)) ? o : {};
            } catch (_) { return {}; }
        }
        const appStore = loadAppStore();
        function saveAppStore() {
            try { localStorage.setItem(APP_STORE_KEY, JSON.stringify(appStore)); }
            catch (e) { console.warn('app store save failed:', e); }
        }
        // One-time migration: pre-remote-AGENTS builds keyed AGENTS.md editors
        // by bare cwd (`app:agents:<cwd>`); the host-qualified scheme is
        // `app:agents:<fileHostId>:<cwd>`. Legacy records were always local, so
        // re-key them to `app:agents:local:<cwd>` — the titlebar button then
        // reuses the same window instead of opening a duplicate, and they
        // restore with an explicit local fileHostId. cwd can contain ':'
        // (Windows drive letters), so detect legacy records by the MISSING
        // fileHostId field rather than by parsing the key. (Codex review.)
        (function migrateAgentsKeys() {
            let changed = false;
            for (const k of Object.keys(appStore)) {
                if (k.indexOf('app:agents:') !== 0) continue;
                const rec = appStore[k];
                if (!rec || rec.fileHostId) continue;   // already host-qualified
                rec.fileHostId = 'local';
                const cwd = rec.agentsMdCwd || k.slice('app:agents:'.length);
                const nk = 'app:agents:local:' + cwd;
                changed = true;
                if (nk === k) continue;
                rec.id = nk;
                // A fresh record already under the new key wins over the stale
                // legacy one; otherwise move the legacy record across.
                if (appStore[nk] === undefined) appStore[nk] = rec;
                delete appStore[k];
            }
            if (changed) saveAppStore();
        })();
        // Snapshot a live app window into a plain store record. The shared
        // serializer for the persisted built-ins (sticky-note / text-editor /
        // file-manager) and the default for an unknown stored kind (#80/S7). Pure:
        // it reads the window and returns the record; saveAppWindow owns the
        // appStore write + the multi-tab capture.
        function serializeAppWindow(win) {
            return {
                id: win.id,
                appKind: win.appKind,
                title: win.name,
                // A live window is open by definition; closeWindow flips this
                // to false so startup + the "closed docs" menu can tell saved-
                // and-closed docs apart from ones still on the taskbar.
                open: true,
                // Route through the content accessor so a CodeMirror-backed
                // editor persists its doc (win.body is the flex container, not a
                // textarea, once CM mounts). Falls back to the textarea value.
                content: win.getContent
                    ? win.getContent()
                    : ((win.body && win.body.value) || ''),
                geom: Object.assign({}, win.geom),
                color: win.color,
                // Pin/tile state: `locked` (pinned float) + the snapshotted
                // float-box so un-tiling a restored window restores its box.
                // Tiling MEMBERSHIP is NOT stored here — findKeyInLayout (over
                // prefs._layout) is authoritative for that.
                locked: !!win.locked,
                floatGeom: win.floatGeom ? Object.assign({}, win.floatGeom) : null,
                // Editor-only: last server file + word-wrap + line-number
                // preference, and (for AGENTS.md editors) the folder whose
                // AGENTS.md/CLAUDE.md this maintains — so reopening the doc
                // restores its special save hook + template checklist.
                filePath: win.filePath || null,
                wrap: !!win.wrap,
                lineNums: !!win.lineNums,
                // Per-sticky-note text size (#19); harmless for other kinds.
                fontSize: win.fontSize,
                // For a multi-tab window store the window's cwd (win.agentsMdCwd
                // is a mirror that's null while the CLAUDE tab is active); the
                // presence of `docs` routes reopen through the tabbed builder.
                agentsMdCwd: win.tabs
                    ? (win.docsCwd || win.agentsMdCwd || null)
                    : (win.agentsMdCwd || null),
                // Multi-tab agent-docs window: per-tab file buffers + the active
                // tab, so reopen rebuilds the tabbed window from cache (parity
                // with today's cached single-doc restore). null for everything
                // else. The Sections tab is synthetic (rebuilt, not serialized).
                docs: win.tabs
                    ? win.tabs.filter(d => d.kind === 'file').map(d => ({
                        name: d.name,
                        filePath: d.filePath || null,
                        content: d.content || '',
                        wrap: !!d.wrap,
                        lineNums: !!d.lineNums,
                        isAgents: !!d.isAgents,
                        // Persist dirty so a buffer that differs from disk reopens
                        // honestly dirty (and re-prompts on close) rather than
                        // looking clean while silently out of sync (Codex review).
                        dirty: !!d.dirty,
                      }))
                    : null,
                activeTab: win.tabs ? win.activeTab : null,
                // Which broker an AGENTS.md editor's file ops dial, so a
                // reopened remote-host editor still targets the right broker
                // (a since-removed host falls back to local via fileHost()).
                fileHostId: win.fileHostId || 'local',
                // Editor-only: the terminal cwd a blank editor was opened at, so
                // its Open/Save dialogs keep opening "where I am" after a reload
                // (#35 review — a saved editor uses filePath's dir instead).
                startDir: win.startDir || '',
                // File-manager-only: the two panes' current dirs (absolute since
                // #35), so a reopened FM lands back where it was. Harmless empty
                // strings for notes/editors (which never read them back).
                fmLeft: win.fmLeft || '',
                fmRight: win.fmRight || '',
                // File-manager-only per-pane host (#46): split view can straddle
                // two brokers, so each pane persists its OWN host. Empty for
                // notes/editors; an old FM record without these back-fills both
                // panes from fileHostId on restore (openFileManagerWindow).
                fmLeftHostId: win.fmLeftHostId || '',
                fmRightHostId: win.fmRightHostId || '',
            };
        }
        // Persist a live app window through its window-kind serializer (#80/S7).
        // Ephemeral kinds (task-manager / control-panel / help: registered with no
        // serialize) are never stored — the old per-kind early returns. An UNKNOWN
        // appKind (no registry entry) still falls back to the shared serializer, so
        // anything but those three is stored exactly as before. A multi-tab editor
        // captures its live doc first so the serialized `docs` are never stale.
        function saveAppWindow(win) {
            if (!win || win.type !== 'app') return;
            const kind = lookupWindowKind(win.appKind);
            if (kind && !kind.serialize) return;
            if (win.tabs && win._captureActiveDoc) {
                try { win._captureActiveDoc(); } catch (_) {}
            }
            appStore[win.id] = (kind && kind.serialize ? kind.serialize
                                                       : serializeAppWindow)(win);
            saveAppStore();
        }
        function deleteAppWindow(id) {
            if (appStore[id] === undefined) return;
            delete appStore[id];
            saveAppStore();
        }
        // Monotonic-ish id source for new app windows. crypto.randomUUID is
        // unavailable on plain http://<LAN-IP>, so use a timestamp+counter.
        let appSeq = 0;
        function newAppId(kind) {
            appSeq += 1;
            return 'app:' + kind + ':' + Date.now().toString(36) + '-' + appSeq;
        }

        // ---- window-kind registry (#80 / S7) ------------------------------
        // A registry of "app window kinds" so a kind lives in ONE place instead of
        // branching across openAppWindow (factory), saveAppWindow (serialize),
        // restoreAppWindows (restore), closeWindow (retain-vs-delete) and the (+)
        // launch menu. Each entry:
        //   { appKind, factory(appData)->win, serialize(win)->record|null,
        //     restore?(record), retainOnClose?(record)->bool, menu? }
        //   menu = { label, launch(), closedItems?()->[menuItem] }
        // The five CORE built-ins (text-editor, file-manager, task-manager,
        // control-panel, help) are registered as defaults (NOT through a mod) so
        // they behave with mods_enabled=false exactly as the old hardcoded branches
        // did; a mod adds a brand-new kind through ctx.registerWindowKind. The
        // historical sixth kind, sticky-note, is now exactly that — the S8 (#81)
        // mods/sticky/ mod registers it through ctx, so it is present only with
        // mods enabled (a pre-existing note still RESTORES mods-off via the
        // unknown-kind openAppWindow fallback; only retain-on-close + its Closed-
        // notes menu + the launcher ride the mod).
        //
        // Held as a memo on the registry getter (NOT a top-level const) so it is
        // free of the TDZ a `const` carries before this fragment executes — the
        // same no-TDZ pattern localInfo._p uses. First touch lazily registers the
        // built-ins; the Map is assigned BEFORE registering so the re-entrant
        // registerWindowKind -> _windowKindRegistry calls can't recurse.
        function _windowKindRegistry() {
            if (!_windowKindRegistry._m) {
                _windowKindRegistry._m = new Map();   // assign first (recursion guard)
                registerBuiltinWindowKinds();
            }
            return _windowKindRegistry._m;
        }
        // Register one window kind. A non-string/empty appKind or a duplicate is a
        // hard error (no silent last-wins, mirroring registerMod) so a mod that
        // collides with a built-in or another mod is rolled back by initMod. close
        // retention only ever sees a record saveAppWindow wrote, so a retainOnClose
        // without a serialize is meaningless — reject it.
        function registerWindowKind(spec) {
            if (!spec || typeof spec !== 'object') {
                throw new Error('registerWindowKind: spec must be an object');
            }
            const appKind = spec.appKind;
            if (typeof appKind !== 'string' || !appKind) {
                throw new Error('registerWindowKind: a non-empty string appKind is required');
            }
            if (typeof spec.factory !== 'function') {
                throw new Error('registerWindowKind[' + appKind
                    + ']: factory(appData) must be a function');
            }
            for (const k of ['serialize', 'restore', 'retainOnClose']) {
                if (spec[k] != null && typeof spec[k] !== 'function') {
                    throw new Error('registerWindowKind[' + appKind + ']: '
                        + k + ' must be a function');
                }
            }
            if (spec.retainOnClose && !spec.serialize) {
                throw new Error('registerWindowKind[' + appKind
                    + ']: retainOnClose requires serialize');
            }
            const reg = _windowKindRegistry();
            if (reg.has(appKind)) {
                throw ModConflictError('registerWindowKind: duplicate appKind "'
                    + appKind + '"');
            }
            const entry = {
                appKind: appKind,
                factory: spec.factory,
                serialize: spec.serialize || null,
                restore: spec.restore || null,
                retainOnClose: spec.retainOnClose || null,
                menu: (spec.menu && typeof spec.menu === 'object') ? spec.menu : null,
            };
            reg.set(appKind, entry);
            return entry;
        }
        // Remove a registered kind (ctx.registerWindowKind teardown). When `entry`
        // is given, only deletes if it is STILL the live registration for that
        // appKind, so a re-register by another owner is never clobbered.
        function deleteWindowKind(appKind, entry) {
            const reg = _windowKindRegistry();
            if (!reg.has(appKind)) return false;
            if (entry && reg.get(appKind) !== entry) return false;
            reg.delete(appKind);
            return true;
        }
        function lookupWindowKind(appKind) {
            if (typeof appKind !== 'string' || !appKind) return undefined;
            return _windowKindRegistry().get(appKind);
        }
        // Registered kinds in registration order (Map insertion order) — drives the
        // (+) launch menu, so built-in order == the old hardcoded menu order.
        function windowKindMenuList() {
            return Array.from(_windowKindRegistry().values());
        }
        // Pre-populate the core built-ins. text-editor uses the notes/editor
        // builder + record shape; file-manager is persisted too. task-manager /
        // control-panel / help are EPHEMERAL (no serialize => never written to
        // appStore, never restored), matching their old early returns. help's
        // open/launch live in mods/help/help.js but are top-level (hoisted)
        // functions present even when mods_enabled=false, so help is a CORE
        // built-in here (NOT a ctx registration) — registering it through the mod
        // would break Help whenever mods are disabled. The SIXTH historical kind,
        // sticky-note, was extracted to mods/sticky/ (#81/S8): it is the first kind
        // a mod adds through ctx.registerWindowKind, so it is registered at
        // loadMods time (appended after these built-ins) rather than here. Every
        // factory/launch is reached through a deferred wrapper so registration
        // never depends on declaration order and a missing help mod degrades to
        // "Help doesn't open", never a registry-wide throw.
        function registerBuiltinWindowKinds() {
            registerWindowKind({
                appKind: 'text-editor',
                factory: function (d) { return openNoteOrEditorWindow(d); },
                serialize: serializeAppWindow,
                menu: { label: '📄 Text editor',
                        launch: function () { return launchTextEditor(); } },
            });
            registerWindowKind({
                appKind: 'file-manager',
                factory: function (d) { return openFileManagerWindow(d); },
                serialize: serializeAppWindow,
                menu: { label: '🗂 File manager',
                        launch: function () { return launchFileManager(); } },
            });
            registerWindowKind({
                appKind: 'task-manager',
                factory: function (d) { return openTaskManagerWindow(d); },
                menu: { label: '🧰 Task manager',
                        launch: function () { return launchTaskManager(); } },
            });
            registerWindowKind({
                appKind: 'control-panel',
                factory: function (d) { return openControlPanelWindow(d); },
                menu: { label: '🎛 Control panel',
                        launch: function () { return launchControlPanel(); } },
            });
            registerWindowKind({
                appKind: 'help',
                factory: function (d) { return openHelpWindow(d); },
                menu: { label: '❓ Help',
                        launch: function () { return launchHelp(); } },
            });
        }

        // One-time migration: pre-multi-host builds keyed per-session prefs
        // by the bare numeric window id. Host-qualify them to the local
        // host so stored geometry/colors survive the upgrade.
        (function migratePrefKeys() {
            let changed = false;
            for (const k of Object.keys(prefs)) {
                if (k.charAt(0) === '_') continue;        // reserved keys
                if (!/^\d+$/.test(k)) continue;
                const nk = 'local:' + k;
                if (!prefs[nk]) prefs[nk] = prefs[k];
                delete prefs[k];
                changed = true;
            }
            if (changed) savePrefs();
        })();

        // Default keyboard shortcuts (task 4). actionId -> canonical combo
        // string (see comboFromEvent). Ctrl+Alt + Arrow/number/letter keeps
        // them clear of plain terminal input — the dispatcher only fires a
        // binding that carries a non-shift modifier. normalizeSettings fills
        // any missing action from here.
        const DEFAULT_KEYBINDINGS = {
            'focus-col-left':  'Ctrl+Alt+ArrowLeft',
            'focus-col-right': 'Ctrl+Alt+ArrowRight',
            'move-col-left':   'Ctrl+Alt+Shift+ArrowLeft',
            'move-col-right':  'Ctrl+Alt+Shift+ArrowRight',
            'workspace-prev':  'Ctrl+Alt+ArrowUp',
            'workspace-next':  'Ctrl+Alt+ArrowDown',
            'workspace-1':     'Ctrl+Alt+1',
            'workspace-2':     'Ctrl+Alt+2',
            'workspace-3':     'Ctrl+Alt+3',
            'workspace-4':     'Ctrl+Alt+4',
            'workspace-5':     'Ctrl+Alt+5',
            'new-terminal':    'Ctrl+Alt+Enter',
            'toggle-tiling':   'Ctrl+Alt+t',
            'close-window':    'Ctrl+Alt+w',
            'minimize-window': 'Ctrl+Alt+m',
            'toggle-fullscreen': 'Ctrl+Alt+f',
            'open-control-panel': 'Ctrl+Alt+p',
        };

        // AGENTS.md section templates offered as checkboxes in the AGENTS.md
        // editor. Each ticked section is stored in the file as a delimited
        // block so it can be removed cleanly on untick; `id` keys the
        // delimiter, `label` names the checkbox, `body` is the inserted markdown.
        // These three are now only the SEED for a user-editable, /state-synced
        // section library (getSections/setSections); normalizeSettings copies
        // them into s.sections the first time a settings blob lacks one.
        const DEFAULT_SECTIONS = [
            { id: 'build', label: 'Build & test commands',
              body: '## Build & test\n\n- build: \n- test: \n' },
            { id: 'style', label: 'Code style',
              body: '## Code style\n\n- \n' },
            { id: 'pr', label: 'PR / commit conventions',
              body: '## PR & commits\n\n- \n' },
        ];
        // Delimiter-safe slug from a label: lowercase, [a-z0-9-] only, de-duped
        // against `taken` (a Set the caller mutates). IDs are immutable once
        // minted — editing a section's label never changes its id, so blocks
        // already written into an AGENTS.md file (<!-- tpl:ID -->) never orphan.
        function slugifySectionId(label, taken) {
            let base = String(label || '').toLowerCase()
                .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
            if (!base) base = 'section';
            let id = base, n = 2;
            while (taken.has(id)) { id = base + '-' + n; n++; }
            taken.add(id);
            return id;
        }
        // Validate/repair a sections array in place-ish: drop malformed entries,
        // coerce label/body to strings, enforce non-empty unique delimiter-safe
        // ids (preserving a valid existing id, minting one from the label
        // otherwise). Order is preserved. An empty array stays empty (the user
        // may have deleted every section); only a MISSING/non-array seeds the
        // defaults (handled by the caller).
        // CARVE-OUT for `auto:true` entries: a freshly-added section whose id is
        // still TRACKING its label (never yet inserted into a file). For those we
        // re-derive the id from the current label on every commit and keep the
        // `auto` flag, so the visible id follows what the user types. Ids freeze
        // (auto dropped) the first time a tick writes the block into a file — see
        // the AGENTS.md checklist onToggle. Legacy/frozen entries carry no `auto`
        // key and behave exactly as before (id preserved once minted), keeping
        // their /state blob byte-identical.
        function normalizeSectionsArray(arr) {
            const taken = new Set();
            const src = [];
            for (const e of (Array.isArray(arr) ? arr : [])) {
                if (!e || typeof e !== 'object' || Array.isArray(e)) continue;
                const label = typeof e.label === 'string' ? e.label : '';
                const body = typeof e.body === 'string' ? e.body : '';
                const auto = e.auto === true;
                const id = (!auto && typeof e.id === 'string')
                    ? e.id.toLowerCase().replace(/[^a-z0-9-]/g, '') : '';
                src.push({ auto, label, body, id });
            }
            // PASS 1 — reserve every FROZEN (non-auto) id FIRST. A frozen id may
            // already anchor a <!-- tpl:ID --> block in a file, so it must win any
            // collision; a label-tracking auto row must never steal it (that would
            // orphan the block). Frozen ids keep today's exact rule: preserve a
            // valid unique id, mint from the label only on empty/collision. With
            // no auto rows this is byte-identical to the old single pass.
            for (const s of src) {
                if (s.auto) continue;
                if (!s.id || taken.has(s.id)) s.id = slugifySectionId(s.label, taken);
                else taken.add(s.id);
            }
            // PASS 2 — mint auto ids AROUND the reserved frozen ids, so a tracking
            // id can only ever land on a free slug (never displace a frozen one).
            for (const s of src) {
                if (s.auto) s.id = slugifySectionId(s.label, taken);
            }
            // Emit in original order; auto rows keep the flag, frozen rows stay
            // byte-identical (no auto key) to avoid /state churn.
            return src.map(s => s.auto
                ? { id: s.id, label: s.label, body: s.body, auto: true }
                : { id: s.id, label: s.label, body: s.body });
        }

