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
            init: function (ctx) {
                // The (+) launch-menu "Sticky note" entry: a new, empty, pinned
                // note (moved verbatim from 76_js_launch_fullscreen.js).
                function launchStickyNote() {
                    openAppWindow({ id: newAppId('note'),
                                    appKind: 'sticky-note', content: '' });
                }
                // Closed app docs (open:false, no live window) — the way to get a
                // closed sticky note back without it squatting on the taskbar. Each
                // entry reopens straight from its stored record (moved verbatim from
                // 76_js_launch_fullscreen.js).
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
                        const label = '📝 ' + (preview ? preview.slice(0, 28) : name);
                        items.push({ label, enabled: true,
                                     action: () => openAppWindow(a) });
                    }
                    return items;
                }
                // Register the sticky-note window kind through ctx (the same core
                // registry the built-ins use; a duplicate appKind throws -> initMod
                // rolls the mod back; teardown removes exactly THIS registration).
                ctx.registerWindowKind({
                    appKind: 'sticky-note',
                    factory: function (d) { return openNoteOrEditorWindow(d); },
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
                        label: '📝 Sticky note',
                        launch: function () { return launchStickyNote(); },
                        closedItems: function () { return closedAppMenuItems(); },
                    },
                });
            },
        });
