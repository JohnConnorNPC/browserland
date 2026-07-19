        // ---- mod: sticky note (S8 / #81) ----------------------------------
        // The sticky-note app window, extracted from core as the FIRST
        // ctx.registerWindowKind mod (#81). It was a core built-in in #80's
        // window-kind registry; the same { factory, serialize, retainOnClose,
        // menu } spec now ships here and is registered through the mod's ctx, so
        // a sticky note is a first-class window everywhere the registry is
        // consulted (open / serialize / retain-on-close / the (+) launch menu /
        // restore) — only its OWNER moved from core to a mod.
        //
        // PERSISTENCE IS BYTE-IDENTICAL (the issue's hard requirement): the spec
        // reuses the EXACT core paths — the shared serializer serializeAppWindow
        // (54_js_app_windows_store.js) writes the same record into the same
        // localStorage blob (webterm:appwindows:v1), the factory delegates to the
        // shared openNoteOrEditorWindow builder (70_js_editor_app.js, still core
        // because the text editor / S10 shares it and it is the unknown-kind
        // fallback), and the retain trim + Closed-notes filter are copied verbatim
        // from the pre-extraction core. Both helpers are hoisted top-level
        // `function` declarations in the one concatenated <script>, so referencing
        // them from this mod's init (which runs from loadMods, after all parsing)
        // is safe — no TDZ.
        //
        // mods_enabled=false posture (same as every extracted mod — theme/pattern/
        // clock/help's chip all go inert when mods are off): the sticky-note kind
        // is simply not registered. A pre-existing persisted note STILL restores
        // on reload, because restoreAppWindows -> openAppWindow falls back to
        // openNoteOrEditorWindow for an unregistered kind (the note opens with its
        // saved geom/color/content). What the kind owns — retain-on-close, the
        // Closed-notes submenu, and the (+) "Sticky note" launcher — is what goes
        // away when its mod is disabled; closing a note then discards it like any
        // unknown-kind app window. The broker-level mods kill switch is off by
        // default, so this is the deliberate "I turned the mods off" degradation,
        // not a default-path regression.
        registerMod({
            id: 'sticky',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['settings', 'window'],   // #86: the #141 taskbar toggle (ctx.settings.boolean) + the sticky-note window kind (ctx.registerWindowKind)
            init: function (ctx) {
                // The (+) launch-menu "Sticky note" entry: a new, empty, pinned
                // note (moved verbatim from 76_js_launch_fullscreen.js).
                function launchStickyNote() {
                    openAppWindow({ id: newAppId('note'),
                                    appKind: 'sticky-note', content: '' });
                }
                // Closed app docs (open:false, no live window) — the way to get a
                // closed sticky note back without it needing a taskbar chip (notes
                // stay off the taskbar unless the #141 toggle below opts them in).
                // Each entry reopens straight from its stored record (moved
                // verbatim from 76_js_launch_fullscreen.js).
                function closedAppMenuItems() {
                    // Issue #11: the closed-docs list holds ONLY non-empty sticky
                    // notes. closeWindow discards every other kind on close, so this
                    // filter is also the defensive guard that hides any stale legacy
                    // records (closed editors/file-managers/empty notes left in the
                    // store from before this change) without a destructive one-time
                    // purge.
                    const closed = Object.keys(appStore)
                        .map(k => appStore[k])
                        .filter(a => a && a.open === false && !windows.has(String(a.id))
                            && a.appKind === 'sticky-note'
                            && String(a.content == null ? '' : a.content).trim());
                    if (!closed.length) return [];
                    const items = [{ sep: true },
                                   { label: 'Closed notes', enabled: false }];
                    for (const a of closed) {
                        // Prefer a content preview so identical default titles stay
                        // distinguishable; fall back to the title.
                        const preview = String(a.content == null ? '' : a.content)
                            .replace(/\s+/g, ' ').trim();
                        const name = a.title || 'Sticky Note';
                        const label = (preview ? preview.slice(0, 28) : name);
                        // #119: the SVG note icon replaces the old '📝 ' prefix;
                        // renderMenu resolves iconKey to the trusted registry glyph.
                        items.push({ label, enabled: true, iconKey: 'sticky',
                                     action: () => openAppWindow(a) });
                    }
                    return items;
                }
                // ---- notes on the taskbar (#141) --------------------------
                // Sticky notes are the one window kind whose chip the shared
                // builder skips (editor.js gates on isNote — notes default
                // always-on-top, so a chip is usually noise). This toggle opts
                // them back in. The builder already creates the synthetic
                // kind:'app' session entry for notes (which is also what
                // shields a chip from the poll reaper), and the rename/color
                // paths already call updateTaskbarLabel/Color unconditionally
                // — so a chip added here tracks title + color with no extra
                // wiring, and closeWindow's app branch removes it on close.
                function noteWindows() {
                    return Array.from(windows.values()).filter(function (w) {
                        return w && w.type === 'app'
                            && w.appKind === 'sticky-note' && !w.disposed;
                    });
                }
                // The same idempotent add recipe every app-window kind uses
                // (clipboard/task-manager/...): append-if-absent, then fix the
                // accent + label (buildTaskbarItem colors from prefs, which
                // app windows don't use).
                function addChip(id) {
                    const sess = sessions.get(id);
                    if (!sess) return;
                    const itemsHost = document.getElementById('taskbar-items');
                    if (!itemsHost) return;
                    if (!itemsHost.querySelector(
                            '.taskbar-item[data-session-id="'
                            + cssEscape(id) + '"]')) {
                        itemsHost.appendChild(buildTaskbarItem(sess));
                    }
                    updateTaskbarColor(id);
                    updateTaskbarLabel(id);
                    // Sync active/minimized classes NOW — a chip added right
                    // after the builder focused the note would otherwise read
                    // inactive until the next taskbar refresh (codex).
                    updateTaskbarActive();
                    const emptyMsg = document.getElementById('taskbar-empty');
                    if (emptyMsg) emptyMsg.remove();
                }
                // Remove ONLY the chip — the synthetic session entry stays
                // (formatTitle / applyDisplaySettings rely on it, exactly the
                // pre-#141 posture of an open note). A note minimized via its
                // chip is un-minimized FIRST: with the chip gone there is no
                // taskbar affordance to restore it, and a minimized-but-open
                // window is invisible everywhere else (not in Closed notes) —
                // the stranded-note trap (codex).
                function removeChip(id) {
                    const w = windows.get(id);
                    if (w && w.minimized) restoreWindow(id);
                    const el = document.querySelector(
                        '.taskbar-item[data-session-id="'
                        + cssEscape(id) + '"]');
                    if (el) el.remove();
                }
                // Default OFF = the key absent from the synced blob = today's
                // behavior. onChange fires on a local toggle AND on a
                // cross-browser /state convergence, so every browser viewing
                // this broker applies the flip to its open notes live.
                const taskbarSetting = ctx.settings.boolean(
                    'stickyTaskbar', false, {
                        title: 'Sticky notes',
                        label: 'show sticky-note windows on the taskbar',
                        isBrowserGlobal: true,
                    });
                taskbarSetting.onChange(function (on) {
                    for (const w of noteWindows()) {
                        if (on) addChip(w.id);
                        else removeChip(w.id);
                    }
                    updateTaskbarActive();
                });
                // Notes already open when this init runs get their chips NOW:
                // restoreAppWindows runs BEFORE loadMods on a reload (restored
                // notes go through the unknown-kind fallback, never this mod's
                // factory), and a mid-session re-enable replays here too. The
                // add-only pass (no remove) keeps init side-effect-free when
                // the toggle is off.
                if (taskbarSetting.get()) {
                    for (const w of noteWindows()) addChip(w.id);
                }

                // Register the sticky-note window kind through ctx (the same core
                // registry the built-ins use; a duplicate appKind throws -> initMod
                // rolls the mod back; teardown removes exactly THIS registration).
                // The factory wrapper is the ONE seam every note-open path funnels
                // through — (+) launch, restore-on-reload, Closed-notes reopen,
                // and a chip click on a closed note — so chip creation here
                // covers them all.
                ctx.registerWindowKind({
                    appKind: 'sticky-note',
                    factory: function (d) {
                        const win = openNoteOrEditorWindow(d);
                        if (win && taskbarSetting.get()) addChip(win.id);
                        return win;
                    },
                    serialize: serializeAppWindow,
                    retainOnClose: function (rec) {
                        // Issue #11: keep ONLY a non-empty sticky note, content
                        // trimmed; an empty note is discarded. Mutates the record in
                        // place (closeWindow saves the store right after).
                        const content = String(rec.content == null ? '' : rec.content).trim();
                        if (!content) return false;
                        rec.content = content;
                        return true;
                    },
                    menu: {
                        label: 'Sticky note',
                        iconKey: 'sticky',   // #119: SVG yellow note in the (+) menu
                        launch: function () { return launchStickyNote(); },
                        closedItems: function () { return closedAppMenuItems(); },
                    },
                });

                // Mod disable: open notes deliberately stay open (they always
                // have — the kind unregisters, the windows remain), but the
                // chips are this mod's feature, so take them with us. The
                // settings control removes itself via the ctx primitive's own
                // unload; the stored value is inert until re-enable.
                ctx.onUnload(function () {
                    for (const w of noteWindows()) removeChip(w.id);
                    updateTaskbarActive();
                });
            },
        });
