        // ---- workspaces (#148, extracted from core 62_js_workspaces.js) ----
        // Vertical workspaces as a first-class mod. Default ON: with it enabled
        // the desktop behaves exactly as it did when this lived in core.
        //
        // THE WHOLE DESIGN IN ONE LINE: workspaces are PRESENTATION over the one
        // desktop core owns. Every tiled column lives in prefs._layout.columns
        // exactly once, always; this mod stamps a `wsId` on each column and
        // registers ctx.desktop.columnFilter, so the strip draws one group at a
        // time. Nothing is ever moved between stores, which is why disabling is
        // non-destructive AND reversible: drop the filter and every window is
        // back on one desktop, re-enable and the groups return. There is no
        // flatten, and no way for a browser without this mod to destroy the
        // workspaces of a browser that has it.
        //
        // Store (rides the /state-synced layout blob, like _layout.openTerms --
        // core passes unknown fields through untouched, so it is shared across
        // every browser on this broker with no broker or wire change):
        //   _layout.mods.workspaces = { version:1, activeId,
        //                               list:[ {id, name?, focusedCol:int,
        //                                       focusedColId?:str,
        //                                       focusedColIdAt?:int} ] }
        //   (#247: focusedColId names the focused column, trusted only while
        //   focusedCol still equals focusedColIdAt -- a browser without this
        //   code writes only the index, which then wins.)
        //   #258 adds, all optional (absent = today's behaviour, so version
        //   stays 1 -- an older build writes `version = 1` on every read):
        //     list[i].color?:'#rrggbb'   list[i].stack?:'<stack id>'
        //     stacks?:[ {id, name?, color?:'#rrggbb', lastId?:'<member id>'} ]
        //   Order is the list order; a stack's members are contiguous in it.
        //   _layout.columns[i].wsId = '<workspace id>'   (mod-owned; core
        //                               preserves it and never reads it)
        // An UNSTAMPED column -- one core made while this was off, or one a peer
        // browser without the mod created -- is adopted by the ACTIVE workspace
        // on sight, so nothing is ever orphaned.
        //
        // Floating windows are a separate axis: their membership lives in the
        // browser-LOCAL prefs._floatWs (key -> wsId | null = all workspaces),
        // because per-window placement is per-browser like floating geometry.
        //
        // Direct core access is idiomatic here (the loader is explicit that the
        // ctx families are review hygiene, not a boundary): windows, prefs,
        // getLayout, visibleColumns, relayoutStrip, parkWindow, savePrefs,
        // renderMenu, openTextPrompt/openConfirmDialog are all in the same
        // scope. ctx is used where a real seam exists -- the column filter, the
        // menus, the key actions, the taskbar hooks and the two settings.
        registerMod({
            id: 'workspaces',
            version: '1.0.0',
            ctxVersion: 1,
            // window: masks/parks windows and adds title-bar menu items;
            // taskbar: badges + dims chips and intercepts activation;
            // settings: owns wsLabelMode + hideTaskbarOtherWs + hideWsPager.
            tiers: ['window', 'taskbar', 'settings'],
            init: function (ctx) {
            //: #196/#225: ctx.prefs for this activation, or null. Declared at
            //: the TOP of init -- above the assignment below and above every
            //: function that reads it -- because a `let` read before its
            //: declaration line has run is a TDZ ReferenceError that disables
            //: the whole mod, and this JS never runs in CI (#162).
            let wsPrefs = null;
            //: #195/#225: the HOME lease, mirrored off 'lease:changed'.
            let _leaseSubscribed = false;
            let _leaseLost = false;
            //: "another browser holds the lease, so do not write shared state".
            //: The event when we have it, core's own private read when this
            //: build has no event bus at all.
            function leaseLost() {
                if (_leaseSubscribed) return _leaseLost;
                try { return _deactivated === true; } catch (_) { return false; }
            }
                // #196/#225: the per-mod prefs store, stashed for floatWsMap.
                // Null on a build without the extender, which is the branch
                // that keeps using the reserved prefs._floatWs key.
                wsPrefs = (ctx.prefs && typeof ctx.prefs.get === 'function')
                    ? ctx.prefs : null;

                // #195/#225: THE LEASE, AS AN EVENT. This mod read core's
                // private `_deactivated` to answer "is another browser holding
                // the HOME lease, so I must not write?". 64 names this mod as
                // the reason the channel exists, and it also settles the edge
                // that makes the swap safe rather than merely tidier: the emit
                // is at the one place core LEARNS the lease, and by the time a
                // handler runs a LOSS has fully torn the view down
                // (teardownView is synchronous) and a GAIN has already set
                // _deactivated=false (bootActiveView and rebuildView both do it
                // before their first await). So `!active` at handler time is
                // exactly what the private read answered.
                //
                // It is a LEVEL, so subscribing replays the current value at
                // once -- this mod does not have to guess a starting state, and
                // `_leaseLost` below is only ever written by the handler.
                if (ctx.events && typeof ctx.events.on === 'function') {
                    _leaseSubscribed = true;
                    ctx.events.on('lease:changed', function (p) {
                        _leaseLost = !(p && p.active === true);
                    });
                }

                // The three settings this mod owns, read through onto the SAME
                // synced blob they always lived in (no new schema field, the
                // #126 termfont pattern) -- an upgrading user's stored value is
                // preserved and no key is written by a mere read.
                //
                // A `title:` on the FIRST one only: ctx.settings.* gives every
                // control its own .set-section, and they are appended to
                // #set-mods in creation order, so one mod's controls are always
                // contiguous -- the heading labels the block (the aistatus mod
                // does the same). Later ones pass none, or each would repeat it.
                const labelMode = ctx.settings.select('wsLabelMode', [
                    { value: 'number', label: 'Numbers' },
                    { value: 'name', label: 'Names' },
                ], { title: 'Workspaces', label: 'Workspace labels', def: 'number',
                     mount: 'windows' });
                // #181: EVERY call site takes the same mount, not just the titled
                // one. The contiguity these three rely on is DOM order inside one
                // applet -- placing only the titled section would strand the two
                // untitled ones in the Mods bucket under no heading at all.
                const hideOther = ctx.settings.boolean('hideTaskbarOtherWs', false, {
                    label: 'Hide taskbar items from other workspaces',
                    mount: 'windows',
                });
                // #162: hide JUST the pager, for people who drive workspaces
                // from the keyboard and want the taskbar space back.
                //
                // This is NOT the per-mod visibility toggle #101/#102 removed.
                // Those went on the rule "the chip's lifecycle IS the mod's
                // lifecycle" -- for the clock and help chips the chip is the
                // whole mod, so the enable switch already WAS the show/hide
                // control. Workspaces is not that shape: disabling the mod also
                // collapses every workspace onto one desktop, drops the taskbar
                // badges and dimming, removes Send to workspace / On all
                // workspaces from the title-bar menus, unmasks floating windows
                // that were hiding, and delists the workspace shortcuts. "Disable
                // the mod" and "hide the pager" are genuinely different asks.
                //
                // Hide-prefixed and default-off to match hideTaskbarOtherWs: an
                // absent key means today's behaviour.
                const hidePager = ctx.settings.boolean('hideWsPager', false, {
                    label: 'Hide the workspace pager from the taskbar',
                    mount: 'windows',
                });
                function wsLabelMode() { return labelMode.get(); }
                function setWsLabelModeValue(v) { labelMode.set(v); }
                function hideTaskbarOtherWs() { return hideOther.get(); }
                labelMode.onChange(function () {
                    renderWorkspaces(); applyTaskbarWorkspace();
                });
                hideOther.onChange(function () { applyTaskbarWorkspace(); });
                hidePager.onChange(function () { applyPagerVisibility(); });

                // The pager lives in the taskbar between the chips and the host
                // status, exactly where the core markup used to put it. Created
                // here and removed on unload -- the #clock-chip pattern.
                const pager = document.createElement('div');
                pager.id = 'ws-pager';
                const taskbar = document.getElementById('taskbar');
                const hostStatus = document.getElementById('host-status');
                if (taskbar) {
                    if (hostStatus) taskbar.insertBefore(pager, hostStatus);
                    else taskbar.appendChild(pager);
                }
                // Registered the instant the node is in the DOM, BEFORE anything
                // below can throw: initMod rolls a failed init back by running
                // the unloads it got, so a throw in between would strand this
                // pager in the taskbar with no mod behind it. Same reason
                // _controlSection registers its own removal before the caller
                // builds a widget.
                ctx.onUnload(function () { pager.remove(); });
                // #162: ONE node for the mod's lifetime, shown/hidden by
                // display. Deliberately not remove-and-recreate: the unload
                // above closes over THIS node, so after one hide/show cycle
                // teardown would remove a detached node and leave a live pager
                // behind, its dot handlers still calling switchWorkspace inside
                // a torn-down mod. display:none (not "render nothing into it")
                // because #taskbar is a flex row with gap:6px, so an empty but
                // present pager would still eat a gap slot -- the same trap core
                // fixed for #host-status with `:empty { display: none; }`.
                //
                // `atInit` exists for one reason: hideWsPreview() reads the
                // `let _wsPreviewEl` declared further down, which is still in its
                // temporal dead zone while init runs. Calling it from here would
                // throw ReferenceError -- and because that throw happens inside
                // init, it would DISABLE THE WHOLE MOD for anyone who had the
                // setting on, on every load. There is provably no preview to
                // strand at creation: nothing has hovered a dot yet.
                function applyPagerVisibility(atInit) {
                    const hide = hidePager.get();
                    pager.style.display = hide ? 'none' : '';
                    // The hover preview is a document.body child with
                    // pointer-events:none whose only removers are a dot's
                    // mouseleave, the next showWsPreview and unload. Hiding
                    // while the pointer rests on a dot -- which happens for free
                    // when the value arrives from another browser on the poll --
                    // would strand the thumbnail on screen until a reload.
                    if (hide && !atInit) hideWsPreview();
                }
                // Apply the stored value at CREATION: onChange never fires at
                // init (the control entry is seeded with last: read()), so
                // otherwise a user who hid the pager would see it again on
                // every load.
                applyPagerVisibility(true);
            // Set by any wsStore()/adopt heal that mutated the store without
            // saving; adoptUnstampedColumns() is the single place that turns it
            // into one savePrefs().
            let _wsDirty = false;
            function wsStore() {
                const L = getLayout();
                if (!L.mods || typeof L.mods !== 'object' || Array.isArray(L.mods)) {
                    L.mods = {};
                }
                let st = L.mods.workspaces;
                if (!st || typeof st !== 'object' || Array.isArray(st)) {
                    st = L.mods.workspaces = {};
                }
                st.version = 1;
                if (!Array.isArray(st.list)) st.list = [];
                // Heal the entries, dropping garbage and duplicate ids. IN PLACE:
                // a caller may hold st.list across something that re-reads the
                // store, and a swapped-out array would orphan its mutation --
                // the same hazard reconcileLayout has with L.columns.
                const seen = new Set();
                const kept = st.list.filter(function (w) {
                    if (!w || typeof w !== 'object' || Array.isArray(w)) return false;
                    if (typeof w.id !== 'string' || !w.id || seen.has(w.id)) return false;
                    seen.add(w.id);
                    if (typeof w.name !== 'string' || !w.name.trim()) delete w.name;
                    else w.name = w.name.slice(0, 40);
                    if (!Number.isInteger(w.focusedCol) || w.focusedCol < 0) w.focusedCol = 0;
                    if (healColor(w)) _wsDirty = true;
                    if (w.focusedColId !== undefined
                            && (typeof w.focusedColId !== 'string' || !w.focusedColId
                                || !Number.isInteger(w.focusedColIdAt))) {
                        delete w.focusedColId;
                        delete w.focusedColIdAt;
                    }
                    return true;
                });
                if (kept.length !== st.list.length) _wsDirty = true;
                st.list.length = 0;
                for (const w of kept) st.list.push(w);
                // #148 hand-off: core's one-time legacy migration left the old
                // grouping (ids only) under L.wsLegacy. Adopt it once, then delete.
                if (L.wsLegacy && typeof L.wsLegacy === 'object') {
                    adoptLegacyWorkspaces(L, st);
                    delete L.wsLegacy;
                    // The adopt rebuilt the list AND stamped columns, none of it
                    // saved yet. Flag it: without this the whole migration lives
                    // in memory only and is redone from the server's blob on
                    // every load, losing any stamp made since.
                    _wsDirty = true;
                }
                if (!st.list.length) {
                    st.list.push({ id: mintLayoutId(), focusedCol: 0 });
                    // Freshly seeded. Flag it so it is persisted once: an id that
                    // only ever lived in memory is re-minted on the next load, and
                    // every column stamped with the old one would read as dangling
                    // and lose its grouping.
                    _wsDirty = true;
                }
                if (healStacks(st)) _wsDirty = true;
                if (!st.list.some(function (w) { return w.id === st.activeId; })) {
                    st.activeId = st.list[0].id;
                    _wsDirty = true;
                }
                return st;
            }
            // #258: a colour is a canonical '#rrggbb' (lower case); a 3-digit
            // or upper-case hex is canonicalised, anything else is no colour.
            function wsHex(c) {
                if (typeof c !== 'string') return null;
                if (/^#[0-9a-fA-F]{6}$/.test(c)) return c.toLowerCase();
                if (/^#[0-9a-fA-F]{3}$/.test(c)) {
                    return ('#' + c[1] + c[1] + c[2] + c[2] + c[3] + c[3]).toLowerCase();
                }
                return null;
            }
            // Heal o.color in place; true when it wrote (a canonical one is
            // left alone, so a valid adopted blob is never rewritten).
            function healColor(o) {
                if (o.color === undefined) return false;
                const c = wsHex(o.color);
                if (c === o.color) return false;
                if (c === null) delete o.color; else o.color = c;
                return true;
            }
            // #258 stacks, healed in a FIXED order: the stack rows, each
            // member's w.stack, contiguity, dissolving, then lastId. True when
            // anything was rewritten. Nothing is ever ADDED to a valid store
            // (no `stacks: []` materialised), and a second pass is a no-op.
            function healStacks(st) {
                let dirty = false;
                if (st.stacks !== undefined && !Array.isArray(st.stacks)) {
                    delete st.stacks;
                    dirty = true;
                }
                const rows = new Map();
                if (Array.isArray(st.stacks)) {
                    const wsIds = new Set(st.list.map(function (w) { return w.id; }));
                    const kept = st.stacks.filter(function (s) {
                        if (!s || typeof s !== 'object' || Array.isArray(s)) return false;
                        // A stack id must not collide with a workspace id:
                        // every lookup is typed, but the store must not lie.
                        if (typeof s.id !== 'string' || !s.id || rows.has(s.id)
                            || wsIds.has(s.id)) return false;
                        rows.set(s.id, s);
                        if (s.name !== undefined) {
                            const t = (typeof s.name === 'string') ? s.name.trim().slice(0, 40) : '';
                            if (t !== s.name) { if (t) s.name = t; else delete s.name; dirty = true; }
                        }
                        if (healColor(s)) dirty = true;
                        return true;
                    });
                    if (kept.length !== st.stacks.length) {
                        st.stacks.length = 0;
                        for (const s of kept) st.stacks.push(s);
                        dirty = true;
                    }
                }
                for (const w of st.list) {
                    if (w.stack !== undefined && !(typeof w.stack === 'string' && rows.has(w.stack))) {
                        delete w.stack;
                        dirty = true;
                    }
                }
                // Members are CONTIGUOUS, which is what makes the flat order
                // the pager's reading order. Stable group-at-first-occurrence:
                // a stack's members all land where its first member stands,
                // in list order; plain workspaces keep their place.
                const order = [];
                const placed = new Set();
                for (const w of st.list) {
                    if (!w.stack) { order.push(w); continue; }
                    if (placed.has(w.stack)) continue;
                    placed.add(w.stack);
                    for (const m of st.list) if (m.stack === w.stack) order.push(m);
                }
                if (order.some(function (w, i) { return w !== st.list[i]; })) {
                    st.list.length = 0;              // in place, like the entry heal
                    for (const w of order) st.list.push(w);
                    dirty = true;
                }
                // A stack left with one workspace dissolves on its own.
                const size = new Map();
                for (const w of st.list) if (w.stack) size.set(w.stack, (size.get(w.stack) || 0) + 1);
                for (const w of st.list) {
                    if (w.stack && size.get(w.stack) < 2) { delete w.stack; dirty = true; }
                }
                if (Array.isArray(st.stacks)) {
                    const live = st.stacks.filter(function (s) { return (size.get(s.id) || 0) >= 2; });
                    if (live.length !== st.stacks.length) {
                        st.stacks.length = 0;
                        for (const s of live) st.stacks.push(s);
                        dirty = true;
                    }
                    // LAST: the last-used member must still be a member.
                    for (const s of st.stacks) {
                        if (s.lastId !== undefined && !st.list.some(function (w) {
                            return w.id === s.lastId && w.stack === s.id; })) {
                            delete s.lastId;
                            dirty = true;
                        }
                    }
                }
                return dirty;
            }
            // Rebuild the workspace list from the pre-#148 blob core flattened. The
            // columns are already all in L.columns; this only re-stamps the grouping.
            function adoptLegacyWorkspaces(L, st) {
                const legacy = L.wsLegacy;
                const list = Array.isArray(legacy.list) ? legacy.list : [];
                const byId = new Map();
                for (const col of L.columns) if (col && col.id) byId.set(col.id, col);
                for (const entry of list) {
                    if (!entry || typeof entry !== 'object') continue;
                    const id = (typeof entry.id === 'string' && entry.id)
                        ? entry.id : mintLayoutId();
                    // An entry we already hold (a partly-adopted blob, or a peer
                    // that got here first) is NOT skipped wholesale: only the list
                    // entry is deduped. Its columnIds must still be stamped below,
                    // or those columns stay unstamped and the active workspace
                    // swallows them.
                    if (!st.list.some(function (w) { return w.id === id; })) {
                        const w = { id: id,
                            focusedCol: Number.isInteger(entry.focusedCol)
                                ? entry.focusedCol : 0 };
                        if (typeof entry.name === 'string' && entry.name.trim()) {
                            w.name = entry.name.slice(0, 40);
                        }
                        st.list.push(w);
                    }
                    for (const cid of (Array.isArray(entry.columnIds) ? entry.columnIds : [])) {
                        const col = byId.get(cid);
                        if (col && typeof col.wsId !== 'string') col.wsId = id;
                    }
                }
                if (st.list.length) {
                    const i = Number.isInteger(legacy.activeWs) ? legacy.activeWs : 0;
                    st.activeId = (st.list[Math.max(0, Math.min(i, st.list.length - 1))]
                        || st.list[0]).id;
                }
            }
            // Claim every column that has no workspace, or points at one that no
            // longer exists, for the ACTIVE workspace -- and PERSIST it.
            //
            // Without this, "unstamped means the active workspace" is only ever a
            // read-time default, so a column created by a browser WITHOUT this mod
            // (or by core while it was disabled) would keep reading as unstamped
            // and show up on every workspace at once. The dangling case is the
            // same story as applyWorkspaceVisibility's heal for floats: a peer can
            // remove a workspace this browser still has columns pointing at.
            // Idempotent, and saves only when something actually changed, so it is
            // safe on the every-relayout path.
            function adoptUnstampedColumns() {
                // A torn-down browser (another one holds the lease) must not write:
                // its push is refused or 409s, the adopt replaces the layout with
                // the peer's still-unstamped one, and the next relayout stamps and
                // saves again -- churn that never converges. Reading is unaffected:
                // columnWsId() already treats an unstamped column as the active
                // workspace, so nothing is mis-drawn while we wait.
                if (leaseLost()) return false;
                const st = wsStore();
                const live = new Set(st.list.map(function (w) { return w.id; }));
                let changed = _wsDirty;
                _wsDirty = false;
                for (const col of getLayout().columns) {
                    if (typeof col.wsId === 'string' && live.has(col.wsId)) continue;
                    col.wsId = st.activeId;
                    changed = true;
                }
                if (changed) savePrefs();
                return changed;
            }
            function wsList() { return wsStore().list; }
            // #258: an id no column, workspace or stack already holds. 57
            // seeds its counter from the stored COLUMN ids only, so after a
            // reload mintLayoutId() can hand back an existing WORKSPACE id --
            // and the dedupe heal then drops the workspace just added.
            function mintWsUniqueId() {
                const st = wsStore();
                const used = new Set(st.list.map(function (w) { return w.id; }));
                for (const s of (Array.isArray(st.stacks) ? st.stacks : [])) used.add(s.id);
                for (const c of getLayout().columns) if (c) used.add(c.id);
                let id = mintLayoutId();
                while (used.has(id)) id = mintLayoutId();
                return id;
            }
            function activeWorkspaceId() { return wsStore().activeId; }
            function activeWorkspaceIndex() {
                const st = wsStore();
                const i = st.list.findIndex(function (w) { return w.id === st.activeId; });
                return i === -1 ? 0 : i;
            }
            function activeWorkspaceEntry() { return wsList()[activeWorkspaceIndex()]; }
            // A column's workspace. Unstamped => the active one (adopted on sight),
            // and so is a column stamped for a workspace that no longer exists.
            function columnWsId(col, st) {
                st = st || wsStore();
                const id = col && col.wsId;
                if (typeof id !== 'string') return st.activeId;
                return st.list.some(function (w) { return w.id === id; }) ? id : st.activeId;
            }
            function workspaceColumns(wsId) {
                const L = getLayout();
                const st = wsStore();
                return L.columns.filter(function (c) { return columnWsId(c, st) === wsId; });
            }
            function workspaceIndexById(id) {
                return wsList().findIndex(function (w) { return w.id === id; });
            }
            // #244: LIVE windows on a workspace -- the count a needs_confirm
            // answer carries and an MCP remove reports as moved. Dormant
            // layout keys still re-stamp with their column, but a key with
            // no live window is not a window the user would be confirming
            // about.
            function wsLiveWindowCount(wsId) {
                let n = floatingOnWs(wsId).length;
                for (const col of workspaceColumns(wsId)) {
                    for (const k of columnKeys(col)) {
                        const w = windows.get(k);
                        if (w && !w.disposed) n += 1;
                    }
                }
                return n;
            }

            // ---- workspace labels + the pager dots ----------------------------
            function wsLabel(ws, i) {
                if (wsLabelMode() === 'name') {
                    return (ws && ws.name) ? ws.name : ('WS' + wsNumber(ws.id));
                }
                return wsNumber(ws.id);
            }
            // #258 pager entries: one per plain workspace, one per stack (its
            // members are contiguous -- the heal guarantees it). The entry
            // number N is what the number keys and the number labels count;
            // a stack member reads N.k. With no stacks N is the flat index+1.
            function wsPagerEntries() {
                const st = wsStore();
                const rows = new Map();
                for (const s of (Array.isArray(st.stacks) ? st.stacks : [])) rows.set(s.id, s);
                const out = [];
                st.list.forEach(function (w, i) {
                    const prev = out[out.length - 1];
                    if (w.stack && prev && prev.stack && prev.stack.id === w.stack) {
                        prev.members.push(w);
                        return;
                    }
                    const s = (w.stack && rows.get(w.stack)) || null;
                    out.push({ key: s ? 's:' + s.id : 'w:' + w.id, stack: s,
                               ws: s ? null : w, members: [w], flat: i, num: out.length + 1 });
                });
                return out;
            }
            function wsNumber(id) {
                for (const e of wsPagerEntries()) {
                    const k = e.members.findIndex(function (m) { return m.id === id; });
                    if (k >= 0) return e.stack ? e.num + '.' + (k + 1) : String(e.num);
                }
                return '?';
            }
            // Build the workspace dots (1..n + '+') into a container. The
            // churn-guard sig lives on the container itself, so two containers keep
            // INDEPENDENT guards (one rendering never short-circuits the other).
            function renderWsDots(container) {
                if (!container) return;
                const st = wsStore();
                const list = st.list;
                const active = activeWorkspaceIndex();
                const mode = wsLabelMode();
                // EVERY visible field is part of the signature, or an edit hits
                // the churn guard and leaves a stale dot: the mode, the active
                // index, and per workspace its id (order -- a dot carries its
                // id), name, colour and stack; per stack its name and colour
                // (#258). JSON, not a join: a plain join lets ['ab','c'] and
                // ['a','bc'] produce the same signature.
                const stacks = Array.isArray(st.stacks) ? st.stacks : [];
                const sig = JSON.stringify([mode, active,
                    list.map(w => [w.id, w.name || '', w.color || '', w.stack || '']),
                    stacks.map(s => [s.id, s.name || '', s.color || ''])]);
                if (container.dataset.sig === sig) {
                    container.querySelectorAll('.ws-dot:not(.add)').forEach((d, i) => {
                        d.classList.toggle('active', i === active);
                    });
                    return;
                }
                container.dataset.sig = sig;
                // The rebuild below drops the dot the pointer may be resting on
                // WITHOUT firing its mouseleave, so its body-level preview would
                // hang around until the next hover or a reload. Same stranding
                // the pager's hide path has to avoid (#162).
                hideWsPreview();
                container.innerHTML = '';
                list.forEach((ws, i) => {
                    const d = document.createElement('div');
                    d.className = 'ws-dot' + (i === active ? ' active' : '')
                        + (mode === 'name' ? ' named' : '');
                    d.textContent = wsLabel(ws, i);
                    d.dataset.wsId = ws.id;
                    wireEntryDrag(d, 'w:' + ws.id);
                    // #258: the colour tints the dot; its number or name stays.
                    if (ws.color) {
                        d.classList.add('tinted');
                        d.style.setProperty('--ws-color', ws.color);
                    }
                    d.title = (ws.name ? ws.name + ' — ' : '') + 'workspace ' + wsNumber(ws.id)
                        + '  (right-click: rename / remove)';
                    d.addEventListener('click', () => switchWorkspace(i));
                    d.addEventListener('contextmenu', (e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        buildWorkspaceMenu(ws.id, e.clientX, e.clientY);
                    });
                    d.addEventListener('mouseenter', () => showWsPreview(i, d));
                    d.addEventListener('mouseleave', hideWsPreview);
                    container.appendChild(d);
                });
                const add = document.createElement('div');
                add.className = 'ws-dot add';
                add.textContent = '+';
                add.title = 'new workspace';
                add.addEventListener('click', () => addWorkspace());
                container.appendChild(add);
            }

            // ---- workspace rename / remove / label-mode (tasks 18, 19) --------
            // #258: built for a workspace ID and every action re-resolves it
            // at click time -- a menu can sit open across a poll that moves
            // or removes workspaces. A stack member (opened from its stack's
            // list) moves up/down inside the stack instead of left/right.
            function buildWorkspaceMenu(wsId, x, y) {
                const list = wsList();
                const ws = list.find(w => w.id === wsId);
                if (!ws) return;
                const mode = wsLabelMode();
                const key = 'w:' + wsId;
                const at = (fn) => () => { const i = workspaceIndexById(wsId); if (i >= 0) fn(i); };
                const items = [
                    { label: 'Workspace ' + wsNumber(wsId) + (ws.name ? ' — ' + ws.name : ''),
                      enabled: false },
                    { label: 'Rename…', enabled: true, action: at(renameWorkspace) },
                    { label: 'Remove workspace', enabled: list.length > 1,
                      action: at(removeWorkspace) },
                    { sep: true },
                    { label: 'Colour…', enabled: true,
                      action: () => openColorMenu((wsList().find(w => w.id === wsId) || {}).color,
                          x, y, (c) => setWorkspaceColorById(wsId, c)) },
                    { label: 'Stack with…', enabled: wsPagerEntries().length > 1,
                      action: () => openStackWithMenu(wsId, x, y) },
                    ...(ws.stack ? [{ label: 'Remove from stack', enabled: true,
                                      action: () => unstackById(wsId) }] : []),
                    { label: ws.stack ? 'Move up' : 'Move left', enabled: wsCanMove(key, -1),
                      action: () => moveEntry(key, -1) },
                    { label: ws.stack ? 'Move down' : 'Move right', enabled: wsCanMove(key, 1),
                      action: () => moveEntry(key, 1) },
                    { sep: true },
                    { label: (mode === 'name' ? '✓ ' : '   ') + 'Show names',
                      enabled: true, action: () => setWsLabelMode('name') },
                    { label: (mode === 'number' ? '✓ ' : '   ') + 'Show numbers',
                      enabled: true, action: () => setWsLabelMode('number') },
                    { sep: true },
                    { label: 'New workspace', enabled: true, action: addWorkspace },
                ];
                renderMenu(items, x, y);
            }
            function setWsLabelMode(mode) {
                setWsLabelModeValue((mode === 'name') ? 'name' : 'number');
                renderWorkspaces();
                applyTaskbarWorkspace();
            }
            // ---- prompt-free primitives (#244) ------------------------------
            // The UI closures (prompt/confirm first) and the MCP commands
            // (args first) call the SAME primitives, so the two paths cannot
            // drift. Each primitive re-acquires the store and re-locates its
            // workspace BY ID at run time -- a poll adopt can replace
            // prefs._layout wholesale between one path's await and the call.
            // renameWorkspaceById: name is the RAW value (blank clears);
            // returns false when the id is gone.
            function renameWorkspaceById(id, name) {
                const ws2 = wsList().find(w => w.id === id);
                if (!ws2) return false;
                const t = String(name).trim();
                if (t) {
                    ws2.name = t.slice(0, 40);
                    setWsLabelModeValue('name');          // naming implies showing names
                } else {
                    delete ws2.name;
                }
                savePrefs();
                renderWorkspaces();
                applyTaskbarWorkspace();
                return true;
            }
            // addWorkspaceNamed: push (optionally named), persist, render and
            // SWITCH -- addWorkspace's whole behaviour, minus the prompt
            // neither path here has. Name must already be valid (or null).
            function addWorkspaceNamed(name) {
                const st = wsStore();
                const entry = { id: mintWsUniqueId(), focusedCol: 0 };
                if (typeof name === 'string' && name.trim()) {
                    entry.name = name.trim().slice(0, 40);
                }
                st.list.push(entry);
                savePrefs();
                renderWorkspaces();               // new dot shows even before switch
                switchWorkspace(st.list.length - 1);
                return entry;
            }
            // removeWorkspaceById: the post-confirmation body verbatim --
            // re-stamp the victim's columns onto the neighbour (the columns
            // themselves never move, so no window can be lost), re-home its
            // floats, splice, heal activeId. {refused:'last'} | null (gone) |
            // {removed:true}.
            function removeWorkspaceById(id) {
                const st = wsStore();
                if (st.list.length <= 1) return { refused: 'last' };
                const vi = workspaceIndexById(id);
                if (vi < 0) return null;
                const neighbor = st.list[(vi > 0) ? vi - 1 : 1];
                if (!neighbor) return null;
                // #247 item 8: the focused column, before columns merge in.
                const keepId = focusedColumnId();
                for (const col of workspaceColumns(id)) col.wsId = neighbor.id;
                reassignFloatingWs(id, neighbor.id);
                const wasActive = (st.activeId === id);
                st.list.splice(vi, 1);
                if (wasActive) {
                    st.activeId = neighbor.id;
                    applyWorkspaceView();
                } else if (keepId) {
                    // A non-active workspace merged into the active one: its
                    // columns join the visible set, so the index alone would
                    // slide focus onto another column. Keep the same column.
                    const i = visibleColumns().findIndex(function (c) {
                        return c && c.id === keepId; });
                    if (i >= 0) getLayout().focusedCol = i;
                }
                wsStore();        // #258: a stack this left with one member is saved dissolved
                savePrefs();
                renderWorkspaces();
                relayoutStrip();
                applyWorkspaceVisibility();
                applyTaskbarWorkspace();
                updateTaskbarActive();
                return { removed: true };
            }
            // ---- moving workspaces (#258) ---------------------------------
            // An entry is keyed 'w:<id>' (a plain workspace) or 's:<id>' (a
            // whole stack). Each call re-acquires the store and re-locates
            // its keys at RUN time: a drag or a menu can sit across a poll
            // adopt. Order is the list order, so a move is an in-place splice.
            // [start, end) of a CURRENT pager entry's members, else null (a
            // stacked workspace is not an entry of its own).
            function wsBlock(list, key) {
                const id = String(key).slice(2);
                if (String(key).indexOf('s:') === 0) {
                    const a = list.findIndex(function (w) { return w.stack === id; });
                    if (a < 0) return null;
                    let b = a;
                    while (b < list.length && list[b].stack === id) b++;
                    return [a, b];
                }
                const i = list.findIndex(function (w) { return w.id === id; });
                return (i >= 0 && !list[i].stack) ? [i, i + 1] : null;
            }
            function wsMoved() {
                savePrefs();
                renderWorkspaces();
                applyTaskbarWorkspace();           // badges carry the numbers
            }
            // Put entry srcKey before/after entry dstKey. False (and no save)
            // when either is not a current entry or nothing would change.
            function moveEntryTo(srcKey, dstKey, after) {
                const st = wsStore();
                const was = st.list.map(function (w) { return w.id; }).join('\n');
                if (srcKey === dstKey || !wsBlock(st.list, srcKey)
                    || !wsBlock(st.list, dstKey)) return false;
                const src = wsBlock(st.list, srcKey);
                const moved = st.list.splice(src[0], src[1] - src[0]);
                const dst = wsBlock(st.list, dstKey);
                st.list.splice(after ? dst[1] : dst[0], 0, ...moved);
                if (st.list.map(function (w) { return w.id; }).join('\n') === was) return false;
                wsMoved();
                return true;
            }
            // One step (dir -1 / +1): a pager entry passes its neighbouring
            // ENTRY (a plain workspace jumps a whole stack); a stack MEMBER
            // ('w:' of a stacked workspace) swaps inside its stack.
            // Would moveEntry(key, dir) move anything? (menu enabling only;
            // the move itself re-checks.)
            function wsCanMove(key, dir) {
                const list = wsList();
                const i = list.findIndex(function (w) { return 'w:' + w.id === key; });
                if (i >= 0 && list[i].stack) {
                    return !!list[i + dir] && list[i + dir].stack === list[i].stack;
                }
                const es = wsPagerEntries();
                const p = es.findIndex(function (e) { return e.key === key; });
                return p >= 0 && p + dir >= 0 && p + dir < es.length;
            }
            function moveEntry(key, dir) {
                const st = wsStore();
                const i = st.list.findIndex(function (w) { return 'w:' + w.id === key; });
                if (i >= 0 && st.list[i].stack) {
                    const j = i + dir;
                    if (j < 0 || j >= st.list.length || st.list[j].stack !== st.list[i].stack) return false;
                    const w = st.list[i];
                    st.list[i] = st.list[j];
                    st.list[j] = w;
                    wsMoved();
                    return true;
                }
                const es = wsPagerEntries();
                const p = es.findIndex(function (e) { return e.key === key; });
                if (p < 0 || p + dir < 0 || p + dir >= es.length) return false;
                return moveEntryTo(key, es[p + dir].key, dir > 0);
            }
            // Drag a pager entry to reorder (#258). Only OUR drag counts (the
            // custom type: an OS file drag or a stale key is ignored), the
            // half is taken from the DOT's rect (its spans are children), and
            // the drop re-resolves both entries -- a poll can move, remove or
            // stack either while the pointer is down.
            const WS_DND = 'application/x-browserland-ws';
            let _wsDragKey = null;
            function wsDragOurs(e) {
                const types = (e.dataTransfer && e.dataTransfer.types) || [];
                return !!_wsDragKey && Array.prototype.indexOf.call(types, WS_DND) !== -1;
            }
            function wsDropAfter(e) {
                const r = e.currentTarget.getBoundingClientRect();
                return e.clientX > r.left + r.width / 2;
            }
            function wsClearDrop(d) { d.classList.remove('drop-before'); d.classList.remove('drop-after'); }
            function wireEntryDrag(d, key) {
                d.draggable = true;
                d.addEventListener('dragstart', (e) => {
                    _wsDragKey = key;
                    hideWsPreview();
                    if (e.dataTransfer) {
                        e.dataTransfer.setData(WS_DND, key);
                        e.dataTransfer.setData('text/plain', key);   // Firefox needs one
                        e.dataTransfer.effectAllowed = 'move';
                    }
                });
                d.addEventListener('dragover', (e) => {
                    if (!wsDragOurs(e) || _wsDragKey === key) return;
                    e.preventDefault();
                    const after = wsDropAfter(e);
                    d.classList.toggle('drop-before', !after);
                    d.classList.toggle('drop-after', after);
                });
                d.addEventListener('dragleave', (e) => {
                    if (!e.relatedTarget || !d.contains(e.relatedTarget)) wsClearDrop(d);
                });
                d.addEventListener('drop', (e) => {
                    const ours = wsDragOurs(e), src = _wsDragKey;
                    wsClearDrop(d);
                    _wsDragKey = null;
                    if (!ours || src === key) return;
                    e.preventDefault();
                    moveEntryTo(src, key, wsDropAfter(e));
                });
                d.addEventListener('dragend', () => {
                    _wsDragKey = null;
                    pager.querySelectorAll('.ws-dot').forEach(wsClearDrop);
                });
            }
            // ---- colours (#258) -------------------------------------------
            // The launch profiles' palette (core 50) or none -- never a free
            // hex. typeof-guarded: a build without core 50 has no palette.
            function wsPalette() {
                return (typeof PALETTE !== 'undefined' && Array.isArray(PALETTE))
                    ? PALETTE.map(wsHex).filter(Boolean) : [];
            }
            // What a writer may store: a palette hex (any case) -> canonical,
            // null / '' / 'none' -> no colour, anything else -> undefined.
            function wsPickColor(c) {
                if (c === null || c === '' || c === 'none') return null;
                const h = wsHex(c);
                return (h && wsPalette().indexOf(h) !== -1) ? h : undefined;
            }
            function setWorkspaceColorById(id, color) {
                const ws = wsList().find(w => w.id === id);
                const c = wsPickColor(color);
                if (!ws || c === undefined) return false;
                if (c === null) delete ws.color; else ws.color = c;
                savePrefs();
                renderWorkspaces();
                applyTaskbarWorkspace();
                return true;
            }
            // None + one swatch row per palette colour, a check on the
            // current one. The hex is the row's text, so colour is not the
            // only cue here either.
            function openColorMenu(current, x, y, pick) {
                const items = [{ label: (current ? '   ' : '\u2713 ') + 'None', enabled: true,
                                 action: () => pick(null) }];
                for (const c of wsPalette()) {
                    items.push({ label: (c === current ? '\u2713 ' : '   ') + c, enabled: true,
                                 swatch: { color: c }, action: () => pick(c) });
                }
                renderMenu(items, x, y);
            }
            // ---- stacks (#258) --------------------------------------------
            // Stack workspace `id` with the pager entry `targetKey`: join the
            // target's stack (at its end), or form a new stack at the TARGET's
            // place. Leaving another stack first may dissolve that one (the
            // heal). No wsStore() runs between the splices: its heal would see
            // a half-built list (a missing active workspace, a memberless row).
            function stackWith(id, targetKey) {
                const st = wsStore();
                const w = st.list.find(x => x.id === id);
                const key = String(targetKey), tid = key.slice(2);
                const t = key.indexOf('w:') === 0 ? st.list.find(x => x.id === tid) : null;
                let sid = t ? (t.stack || null) : tid;
                if (!w || (t ? t === w : !(st.stacks || []).some(s => s.id === sid))) return null;
                if (w.stack && w.stack === sid) return sid;
                const fresh = !sid;
                if (fresh) sid = mintWsUniqueId();        // before any splice
                st.list.splice(st.list.indexOf(w), 1);
                if (fresh) {
                    if (!Array.isArray(st.stacks)) st.stacks = [];
                    st.stacks.push({ id: sid });
                    t.stack = sid;
                }
                w.stack = sid;
                let end = st.list.findIndex(x => x.stack === sid);
                while (end < st.list.length && st.list[end].stack === sid) end++;
                st.list.splice(end, 0, w);
                wsStore();                                 // an old stack may dissolve
                wsMoved();
                return sid;
            }
            // Out of its stack, to just after the block: spliced out FIRST, so
            // the block end is measured without it. A one-member remainder
            // dissolves in the heal.
            function unstackById(id) {
                const st = wsStore();
                const w = st.list.find(x => x.id === id);
                if (!w || !w.stack) return false;
                const sid = w.stack;
                st.list.splice(st.list.indexOf(w), 1);
                delete w.stack;
                let end = st.list.findIndex(x => x.stack === sid);
                while (end < st.list.length && st.list[end].stack === sid) end++;
                st.list.splice(end, 0, w);
                wsStore();
                wsMoved();
                return true;
            }
            // "Stack with…": every OTHER pager entry, resolved at click.
            function openStackWithMenu(wsId, x, y) {
                const items = [];
                for (const e of wsPagerEntries()) {
                    if (e.members.some(m => m.id === wsId)) continue;
                    const k = e.key;
                    items.push({ enabled: true, action: () => stackWith(wsId, k),
                        label: e.stack ? 'Stack ' + (e.stack.name || e.num) + ' (' + e.members.length + ')'
                                       : (e.ws.name || 'Workspace ' + e.num) });
                }
                renderMenu(items, x, y);
            }
            async function renameWorkspace(i) {
                const ws = wsList()[i];
                if (!ws) return;
                const name = await openTextPrompt({
                    title: 'Rename workspace',
                    label: 'Name',
                    value: ws.name || '',
                    placeholder: 'blank = use the number',
                    okLabel: 'Rename',
                });
                if (name === null) return;          // cancelled
                // A state-sync poll-adopt during the await can swap prefs._layout
                // wholesale, so re-find the live workspace by id before mutating it.
                renameWorkspaceById(ws.id, name);
            }
            async function removeWorkspace(i) {
                if (wsList().length <= 1) {
                    showNotice('cannot remove the last workspace');
                    return;
                }
                const victim = wsList()[i];
                if (!victim) return;
                const hasContent = workspaceColumns(victim.id).length
                    || anyFloatingOnWs(victim.id);
                if (hasContent) {
                    const ok = await openConfirmDialog({
                        title: 'Remove workspace',
                        message: 'Remove workspace ' + wsNumber(victim.id)
                            + '? Its windows move to an adjacent workspace.',
                        okLabel: 'Remove',
                        danger: true,
                    });
                    if (!ok) return;
                }
                // A state-sync poll-adopt during the await can replace prefs._layout
                // wholesale, so the captured list / index go stale. The primitive
                // re-acquires and re-locates the victim by id.
                removeWorkspaceById(victim.id);
            }
            // ---- workspace preview popover (task 19) --------------------------
            let _wsPreviewEl = null;
            //: the ctx.popover handle for the box above, or null (#202/#225).
            let _wsPreviewPop = null;
            function hideWsPreview() {
                if (_wsPreviewPop) { _wsPreviewPop.close(); return; }
                if (_wsPreviewEl) { _wsPreviewEl.remove(); _wsPreviewEl = null; }
            }
            function showWsPreview(i, anchor) {
                hideWsPreview();
                const ws = wsList()[i];
                if (!ws) return;
                const box = document.createElement('div');
                box.className = 'ws-preview';
                const title = document.createElement('div');
                title.className = 'wsp-title';
                title.textContent = (ws.name ? ws.name + '  ' : '')
                    + '(workspace ' + wsNumber(ws.id) + ')';
                box.appendChild(title);
                const map = document.createElement('div');
                map.className = 'wsp-map';
                // Mirror relayoutStrip's live set (61) so the preview never shows a
                // minimized/dormant/phantom ghost the workspace itself doesn't display
                // (#147). Same skeleton as the strip's render set — per column the live
                // rows, and per split row the live cells — each keyed off the SHARED
                // isLiveKey. Dormant keys stay in _layout (reconcile keeps them) but
                // are dropped here exactly as the strip drops them.
                const cols = workspaceColumns(ws.id);
                const renderCols = [];
                for (const col of cols) {
                    const liveRows = [];
                    for (const row of (col.rows || [])) {
                        const liveKeys = rowKeys(row).filter(isLiveKey);
                        if (!liveKeys.length) continue;                 // dormant row -> skip
                        let liveCells = null;
                        if (row.mode === 'split') {
                            // Mirror relayoutStrip (61) exactly: a migrated split uses its
                            // real cells; a legacy cells-less split synthesizes one single-
                            // window pseudo-cell per live key (id === the key, so the by-key
                            // widths lookup still matches) until reconcile migrates it.
                            if (Array.isArray(row.cells)) {
                                liveCells = [];
                                for (const cell of row.cells) {
                                    const ck = (Array.isArray(cell.keys) ? cell.keys : [])
                                        .filter(isLiveKey);
                                    if (!ck.length) continue;           // dead cell -> skip
                                    const at = cell.activeTab;
                                    liveCells.push({ cell, id: cell.id, liveKeys: ck,
                                        repKey: (typeof at === 'string' && ck.indexOf(at) !== -1) ? at : ck[0] });
                                }
                                if (!liveCells.length) continue;
                            } else {
                                liveCells = liveKeys.map(k =>
                                    ({ cell: null, id: k, liveKeys: [k], repKey: k }));
                            }
                        }
                        liveRows.push({ row, liveKeys, liveCells });
                    }
                    if (liveRows.length) renderCols.push({ col, liveRows });
                }
                // Lay the LIVE columns out left-to-right by width fraction, rows top-to-
                // bottom by height fraction — a faithful mini schematic of what shows.
                let xoff = 0;
                const totalFrac = renderCols.reduce((a, rc) => a + colCurrentFrac(rc.col), 0) || 1;
                renderCols.forEach((rc) => {
                    const col = rc.col;
                    const wfrac = colCurrentFrac(col) / Math.max(1, totalFrac);
                    const rowsArr = rc.liveRows;
                    const hsum = rowsArr.reduce((a, lr) => a + (lr.row.height > 0 ? lr.row.height : 0), 0) || 1;
                    let yoff = 0;
                    rowsArr.forEach((lr) => {
                        const row = lr.row;
                        const hfrac = (row.height > 0 ? row.height : (1 / rowsArr.length)) / hsum;
                        // Draw one labeled rect; a tabbed/group tile gets a "(+N)" badge
                        // counting only its HIDDEN LIVE tabs (minimized tabs aren't shown).
                        const mkRect = (left, width, labelKey, plusN) => {
                            const r = document.createElement('div');
                            r.className = 'wsp-rect';
                            r.style.left = (left * 100) + '%';
                            r.style.width = (width * 100) + '%';
                            r.style.top = (yoff * 100) + '%';
                            r.style.height = (hfrac * 100) + '%';
                            const lab = document.createElement('span');
                            const sess = sessions.get(labelKey);
                            let txt = sess ? (sess.title || ('#' + sess.sid))
                                : (windows.has(labelKey)
                                    ? (windows.get(labelKey).name || labelKey) : labelKey);
                            if (plusN > 0) txt += ' (+' + plusN + ')';
                            lab.textContent = txt;
                            r.appendChild(lab);
                            map.appendChild(r);
                        };
                        if (row.mode === 'split' && lr.liveCells) {
                            // One sub-rect per LIVE cell, sized by widths[cell.id] within
                            // the row band but renormalized over the LIVE cells (a dead
                            // cell surrenders its width); a group cell labels its rep live
                            // tab + "(+N)" of its remaining live tabs.
                            const cells = lr.liveCells;
                            const wsum = cells.reduce((a, lc) => {
                                const v = row.widths && Number(row.widths[lc.id]);
                                return a + ((Number.isFinite(v) && v > 0) ? v : 0);
                            }, 0);
                            let cxoff = 0;
                            cells.forEach((lc) => {
                                const v = row.widths && Number(row.widths[lc.id]);
                                const cf = wsum > 0
                                    ? ((Number.isFinite(v) && v > 0) ? v : 0) / wsum
                                    : (1 / cells.length);
                                mkRect(xoff + cxoff * wfrac, cf * wfrac, lc.repKey,
                                    lc.liveKeys.length - 1);
                                cxoff += cf;
                            });
                        } else {
                            // single/tabbed (or a legacy cells-less split): label the
                            // active tab if it's live, else the first live key; badge the
                            // count of the row's remaining LIVE tabs.
                            const at = row.activeTab;
                            const labelKey = (typeof at === 'string'
                                && lr.liveKeys.indexOf(at) !== -1) ? at : lr.liveKeys[0];
                            mkRect(xoff, wfrac, labelKey, lr.liveKeys.length - 1);
                        }
                        yoff += hfrac;
                    });
                    xoff += wfrac;
                });
                const floatN = countFloatingOnWs(ws.id);
                if (!renderCols.length && !floatN) {
                    const e = document.createElement('div');
                    e.className = 'wsp-empty';
                    e.textContent = 'empty';
                    map.appendChild(e);
                }
                box.appendChild(map);
                if (floatN) {
                    const f = document.createElement('div');
                    f.className = 'wsp-title';
                    f.style.marginTop = '4px';
                    f.style.marginBottom = '0';
                    f.textContent = '+ ' + floatN + ' floating';
                    box.appendChild(f);
                }
                // #202/#225: CLAMPING AND FLIPPING ARE CORE'S. This used to
                // append to <body> and place the box by hand -- centre on the
                // dot, clamp with Math.max(6, Math.min(left, innerWidth - bw
                // - 6)), and fall to `ar.bottom + 8` when it would run off the
                // top. ctx.popover.anchor owns all of it, and it FLIPS to the
                // other side rather than sliding a box over its own anchor,
                // which the hand-rolled fall-through only did for the vertical
                // overflow and never for the horizontal one. It also
                // re-measures every frame while open, so the box follows a dot
                // that moves -- which the taskbar's own re-render does.
                _wsPreviewEl = box;
                if (ctx.popover && typeof ctx.popover.anchor === 'function') {
                    _wsPreviewPop = ctx.popover.anchor(box, anchor, {
                        placement: 'top',
                        gap: 8,
                        // Fires for every dismissal, this mod's own close()
                        // included, so it is the one place the refs are
                        // cleared -- and it is what keeps hideWsPreview
                        // idempotent across both paths.
                        onClose: function () {
                            _wsPreviewEl = null;
                            _wsPreviewPop = null;
                        },
                    });
                    return;
                }
                // No popover family on this build: the hand-rolled placement,
                // unchanged, so the preview still appears somewhere sensible.
                document.body.appendChild(box);
                const ar = anchor.getBoundingClientRect();
                const bw = box.offsetWidth, bh = box.offsetHeight;
                let left = ar.left + ar.width / 2 - bw / 2;
                left = Math.max(6, Math.min(left, window.innerWidth - bw - 6));
                let top = ar.top - bh - 8;
                if (top < 6) top = ar.bottom + 8;
                box.style.left = left + 'px';
                box.style.top = top + 'px';
            }
            // ---- floating windows locked to a workspace (task 8) -------------
            // Membership lives in a single reserved map prefs._floatWs (key -> ws
            // id, or null = "all workspaces"). A reserved (_-prefixed) key survives
            // the prefs GC, works for both terminals and app docs without touching
            // their creation paths, and stays browser-local (not pushed to /state —
            // per-window placement is per-browser, like floating pixel geometry).
            // Tiled windows are NOT covered: their membership IS their column.
            // #196/#225: the map lives in ctx.prefs now. The paragraph above
            // is the argument for why a reserved '_'-prefixed key in core's
            // prefs was safe -- it survives the prefs GC and stays out of
            // /state -- and a mod-namespaced record
            // ('webterm:modprefs:workspaces:floatWs') is outside both by
            // construction rather than by discipline.
            //
            // ONE READ-THROUGH, then it never matters again: an upgrading user
            // has their float memberships in prefs._floatWs, and silently
            // scattering every pinned window back to "all workspaces" is a
            // worse outcome than the few lines that carry them over. The
            // migration is a COPY, not a move -- nothing deletes the old key,
            // so a downgrade finds what it left.
            let _floatWsCache = null;
            function floatWsMap() {
                if (_floatWsCache) return _floatWsCache;
                if (wsPrefs) {
                    const stored = wsPrefs.get('floatWs', null);
                    if (stored && typeof stored === 'object'
                            && !Array.isArray(stored)) {
                        _floatWsCache = stored;
                        return _floatWsCache;
                    }
                    // Nothing stored yet: adopt the legacy map if there is one,
                    // and write it back under the new key on the next save.
                    const legacy = prefs._floatWs;
                    _floatWsCache = (legacy && typeof legacy === 'object'
                        && !Array.isArray(legacy))
                        ? Object.assign({}, legacy) : {};
                    return _floatWsCache;
                }
                // No prefs family on this build: the reserved key, unchanged.
                if (!prefs._floatWs || typeof prefs._floatWs !== 'object'
                    || Array.isArray(prefs._floatWs)) prefs._floatWs = {};
                return prefs._floatWs;
            }
            //: Persist the map. ctx.prefs writes localStorage itself, so the
            //: savePrefsLocal the legacy path needs is the caller's, exactly
            //: as before -- see setWindowWs.
            function saveFloatWsMap() {
                if (wsPrefs && _floatWsCache) {
                    wsPrefs.set('floatWs', _floatWsCache);
                    return true;
                }
                return false;
            }
            function keyWsId(key) {
                const v = floatWsMap()[key];
                if (v === null) return null;            // explicit all-workspaces
                return (typeof v === 'string') ? v : undefined;
            }
            function windowWsId(win) { return win ? keyWsId(win.id) : undefined; }
            function setWindowWs(win, wsId, render) {
                if (!win) return;
                floatWsMap()[win.id] = wsId;
                // ctx.prefs writes localStorage itself; the legacy path still
                // needs core's browser-local save. Per-window either way, and
                // never a /state push.
                if (!saveFloatWsMap()) savePrefsLocal();
                if (render) { applyWorkspaceVisibility(); applyTaskbarWorkspace(); }
            }
            function setWindowAllWorkspaces(win, all) {
                setWindowWs(win, all ? null : activeWorkspaceId(), true);
            }
            function floatingOnWs(wsId) {
                const out = [];
                for (const win of windows.values()) {
                    if (win.disposed || win.tiled) continue;
                    if (windowWsId(win) === wsId) out.push(win);
                }
                return out;
            }
            function anyFloatingOnWs(wsId) { return floatingOnWs(wsId).length > 0; }
            function countFloatingOnWs(wsId) { return floatingOnWs(wsId).length; }
            function reassignFloatingWs(fromWsId, toWsId) {
                const m = floatWsMap();
                for (const k of Object.keys(m)) if (m[k] === fromWsId) m[k] = toWsId;
                if (!saveFloatWsMap()) savePrefsLocal();
            }
            // ---- placement / activation helpers (#152) -----------------------
            // Does this float belong to a workspace OTHER than the active one?
            // Answered from MEMBERSHIP, never from the ws-hidden class: the class is
            // a presentation echo that a setWindowWs(render:false) can leave stale in
            // either direction. Tiled (membership IS the column), all-workspaces
            // (null) and unassigned (undefined — adopted on sight) are all "not off".
            function windowOffActiveWs(win) {
                if (!win || win.tiled) return false;
                const wsId = windowWsId(win);
                return (typeof wsId === 'string') && wsId !== activeWorkspaceId();
            }
            // Stamp a floating window's workspace at CREATION, and mask it in the
            // same frame. Without this the stamp only lands later — from
            // applyWorkspaceVisibility on the 2 s taskbar poll, on /state adopt, or
            // inside switchWorkspace — with two consequences: a window that belongs
            // to another workspace paints where you are and flicks away a tick later,
            // and an unassigned window born on ws A just before a switch to B is
            // stamped B (switchWorkspace sets activeId BEFORE it applies visibility).
            function adoptFloatWorkspace(win, hint) {
                if (!win || win.disposed || win.tiled) return;
                // #251: a tiled window an adopted layout floated belongs to the
                // workspace its column was on, not the active one: one parked on
                // a hidden workspace stays hidden instead of popping in here.
                // A stamp left over from an older float is stale, so the hint wins.
                const liveWsIds = new Set(wsList().map(w => w.id));
                if (hint && typeof hint.wsId === 'string' && liveWsIds.has(hint.wsId)) {
                    setWindowWs(win, hint.wsId, false);
                }
                const wsId = windowWsId(win);
                // Reconcile membership exactly as applyWorkspaceVisibility does, or
                // the creation stamp and the poll disagree for two seconds: an
                // unassigned window adopts the active ws, and one pointing at a
                // workspace that no longer exists (removed while this key had no
                // window) is HEALED here. Without the heal, a dangling ref would
                // mask the window against a workspace nothing can switch to — the
                // poll would fix it a tick later, turning "paints then vanishes"
                // into "does nothing, then appears", which is worse in a throttled
                // background tab.
                if (wsId === undefined
                    || (typeof wsId === 'string' && !liveWsIds.has(wsId))) {
                    setWindowWs(win, activeWorkspaceId(), false);   // masked below
                }
                const hide = windowOffActiveWs(win);
                win.dom.classList.toggle('ws-hidden', hide);
                // Same invariant applyWorkspaceVisibility holds: a masked window is
                // never the front window. A window this fresh can't already be front,
                // but this is the only other place the mask is applied, so it owns
                // the rule rather than assuming its callers do.
                if (hide && frontId === win.id) { frontId = null; updateTaskbarActive(); }
                applyTaskbarWorkspace();   // else the chip's ws badge lags a poll
            }
            // Mask floating windows that don't belong to the active workspace;
            // lazily lock an unassigned (new / pre-feature) window to the current
            // workspace. Idempotent — safe to call every poll tick.
            function applyWorkspaceVisibility() {
                const activeId = activeWorkspaceId();
                // Membership ids live in browser-local prefs._floatWs while the
                // workspace SET lives in the /state-synced layout; an adopt that
                // rebuilds the layout (minted fresh ws id) can leave a float
                // pointing at a ws that no longer exists. Treat such a dangling
                // ref like an unassigned window: re-home to the active ws so it
                // shows, and persist the heal so it can't recur. Snapshot the live
                // ws ids once — this runs every poll tick — and note setWindowWs
                // below never mutates the layout, so the set stays valid.
                const liveWsIds = new Set(wsList().map(w => w.id));
                let frontCleared = false;
                for (const win of windows.values()) {
                    if (win.disposed || win.tiled) continue;
                    let wsId = windowWsId(win);
                    if (wsId === undefined) { setWindowWs(win, activeId, false); wsId = activeId; }
                    else if (typeof wsId === 'string' && !liveWsIds.has(wsId)) {  // dangling ref
                        setWindowWs(win, activeId, false); wsId = activeId;  // re-home + heal
                    }
                    const show = (wsId === null) || (wsId === activeId);
                    if (win.dom.classList.contains('ws-hidden') !== !show) {
                        win.dom.classList.toggle('ws-hidden', !show);
                        if (!show && frontId === win.id) { frontId = null; frontCleared = true; }
                        // Masked -> visible: .ws-hidden is display:none, so the
                        // window had no box while parked and sendResize bailed on
                        // its 0x0 rect. Re-measure now it has one, else the PTY
                        // keeps whatever cols/rows it last saw — for a window
                        // created already-masked (#152), that is none at all.
                        else if (show) refitSoon(win);
                    }
                }
                if (frontCleared) updateTaskbarActive();
            }
            // ---- taskbar workspace indicator (task 16) -----------------------
            // Each taskbar item carries a small ws badge and dims when its window
            // lives on another workspace, so the bar indicates the active ws.
            function workspaceIndexForKey(key) {
                const loc = findKeyInLayout(key);
                if (loc) return workspaceIndexById(columnWsId(loc.col));  // tiled membership
                const wsId = keyWsId(key);
                if (typeof wsId === 'string') {
                    const idx = workspaceIndexById(wsId);
                    if (idx >= 0) return idx;
                }
                return null;                            // all-workspaces / unknown
            }
            function applyTaskbarWorkspace() {
                const list = wsList();
                const active = activeWorkspaceIndex();
                const hideOther = !!hideTaskbarOtherWs();
                document.querySelectorAll('#taskbar-items .taskbar-item').forEach(el => {
                    const key = el.dataset.sessionId || '';
                    const wsi = workspaceIndexForKey(key);
                    let badge = el.querySelector('.ti-ws');
                    if (wsi === null || wsi < 0) {
                        if (badge) badge.remove();
                        el.classList.remove('other-ws');
                        el.classList.remove('ws-hidden');   // all-ws items never hidden
                        return;
                    }
                    if (!badge) {
                        badge = document.createElement('span');
                        badge.className = 'ti-ws';
                        el.appendChild(badge);
                    }
                    const ws = list[wsi];
                    badge.textContent = (ws && ws.name) ? ws.name : ('ws' + (ws ? wsNumber(ws.id) : wsi + 1));
                    el.classList.toggle('other-ws', wsi !== active);
                    el.classList.toggle('ws-hidden', hideOther && wsi !== active);
                });
                reorderTaskbarItems();
            }

            // Render the taskbar pager. Called from every workspace-state change,
            // since the pager shows in floating mode too.
            function renderWorkspaces() {
                renderWsDots(document.getElementById('ws-pager'));
            }
            // Push the active workspace's remembered focused column back onto the
            // layout. The strip's filter does the actual showing/hiding.
            function applyWorkspaceView() {
                const L = getLayout();
                const entry = activeWorkspaceEntry();
                const n = workspaceColumns(activeWorkspaceId()).length;
                // #247 item 8: the remembered column by ID, so a column set that
                // changed meanwhile (one sent away, a workspace merged in) still
                // finds it -- trusted only while the index it was recorded with
                // is unchanged (an index-only writer invalidates it).
                if (entry && typeof entry.focusedColId === 'string'
                        && entry.focusedColIdAt === entry.focusedCol) {
                    const i = visibleColumns().findIndex(function (c) {
                        return c && c.id === entry.focusedColId; });
                    if (i >= 0) { L.focusedCol = i; return; }
                }
                L.focusedCol = Math.max(0,
                    Math.min(entry ? entry.focusedCol : 0, Math.max(0, n - 1)));
            }
            // #247 item 8: the visible column focus is on, by id (or null).
            function focusedColumnId() {
                const c = visibleColumns()[getLayout().focusedCol | 0];
                return (c && typeof c.id === 'string') ? c.id : null;
            }
            // Record where focus is in `entry`: the index and, for this code,
            // the column id with the index it was taken at.
            function recordFocus(entry, idx, id) {
                entry.focusedCol = idx;
                if (id) { entry.focusedColId = id; entry.focusedColIdAt = idx; }
                else { delete entry.focusedColId; delete entry.focusedColIdAt; }
            }
            // focusedCol means different things to us and to a browser WITHOUT
            // this mod: it writes an index over ALL columns, we read one over the
            // visible subset, and core's reconcile clamps only against the storage
            // count. Clamp it to what is actually on screen (an out-of-range value
            // focuses nothing at all). It lives in the SYNCED layout, so a real
            // clamp is saved (#246: an unsaved write left the tab dirty with no
            // push queued).
            // This runs on every render, including the one after ADOPTING a
            // peer's state, so it must never save a value derived from valid
            // state -- only repair state that is invalid here. It used to also
            // mirror focusedCol into the active workspace entry; core's focus
            // writes (bringToFront) save without a relayout, so every peer
            // "fixed" that lagging mirror and its save beat this tab's next
            // focus change in a 409 (#246 review, A6). The entry is recorded
            // where it is read instead: switchWorkspace and
            // sendWindowToWorkspace store the active focus before they apply
            // another one.
            function syncFocusedCol() {
                const L = getLayout();
                const n = visibleColumns().length;
                const clamped = Math.max(0, Math.min(L.focusedCol | 0, Math.max(0, n - 1)));
                if (L.focusedCol === clamped) return false;
                L.focusedCol = clamped;
                // Same rule as adoptUnstampedColumns: a torn-down browser never
                // writes shared state (the clamp above still keeps it drawable).
                if (!leaseLost()) savePrefs();
                return true;
            }
            function switchWorkspace(index) {
                const st = wsStore();
                if (index < 0 || index >= st.list.length) return;
                const target = st.list[index];
                if (!target || target.id === st.activeId) return;
                // Park the currently-visible windows first, so the relayout's
                // unused-colEl cleanup never removes a live window's dom.
                for (const col of visibleColumns()) {
                    for (const k of columnKeys(col)) parkWindow(windows.get(k));
                }
                const cur = activeWorkspaceEntry();
                if (cur) recordFocus(cur, getLayout().focusedCol, focusedColumnId());   // remember where we were
                st.activeId = target.id;
                // #258: a stack remembers its last-used member (synced; it
                // rides this switch's save).
                const srow = target.stack && (st.stacks || []).find(s => s.id === target.stack);
                if (srow && srow.lastId !== target.id) srow.lastId = target.id;
                applyWorkspaceView();
                savePrefs();
                renderWorkspaces();               // pager reflects the switch now
                applyWorkspaceVisibility();       // show only this ws's floating wins
                relayoutStrip();                  // mounts the new workspace
                applyTaskbarWorkspace();          // taskbar dims off-ws items
                // Focus the new workspace's focused column.
                const fcol = visibleColumns()[getLayout().focusedCol];
                if (fcol) {
                    // Prefer the focused column's active (visible) tab.
                    const fk = firstLiveKeyInColumn(fcol);
                    if (fk) bringToFront(fk);
                }
            }
            function addWorkspace() {
                return addWorkspaceNamed(null);
            }
            // Move a tiled window to another workspace. Its column is RE-STAMPED
            // (and moved beside that workspace's other columns so the strip and the
            // taskbar keep a sensible left-to-right order) — never copied, so no
            // window can be lost. If the window shared a column, it is expelled into
            // its own column first, exactly as "Move to own column" would.
            function sendWindowToWorkspace(win, targetIndex) {
                if (!win || win.disposed) return;
                const st = wsStore();
                const target = st.list[targetIndex];
                if (!target) return;
                let loc = findKeyInLayout(win.id);
                if (!loc || columnWsId(loc.col, st) === target.id) return;
                const focusPrecap = (frontId === win.id)
                    ? captureTiledFocusContext(win.id) : null;
                const L = getLayout();
                // #247 item 8: where focus is, BEFORE the column set changes.
                const keepId = focusedColumnId();
                const keepIdx = L.focusedCol | 0;
                const shared = (loc.col.rows.length > 1 || rowKeys(loc.row).length > 1);
                if (shared) {
                    expelToNewColumn(win);        // its own column, adjacent to the old
                    loc = findKeyInLayout(win.id);
                    if (!loc) return;
                }
                const col = loc.col;
                col.wsId = target.id;
                // Re-seat it next to the target workspace's last column so storage
                // order stays grouped (the taskbar's spatial order reads it).
                const peers = workspaceColumns(target.id).filter(c => c !== col);
                const at = peers.length
                    ? L.columns.indexOf(peers[peers.length - 1]) + 1 : L.columns.length;
                const from = L.columns.indexOf(col);
                if (from !== -1 && at !== from) {
                    L.columns.splice(from, 1);
                    L.columns.splice(at > from ? at - 1 : at, 0, col);
                }
                const tIdx = Math.max(0, workspaceColumns(target.id).indexOf(col));
                recordFocus(target, tIdx, col.id);
                // applyWorkspaceView below re-applies the ACTIVE entry, which
                // is only recorded on demand (see syncFocusedCol): record the
                // current focus first so it is kept, not rewound. #247: by the
                // column focus was on before the move (an index taken now
                // would point at whatever slid into its slot); if that column
                // is the one sent away, the clamped old index.
                const cur = activeWorkspaceEntry();
                if (cur && cur !== target) {
                    const vis = visibleColumns();
                    let ni = keepId ? vis.findIndex(function (c) { return c && c.id === keepId; }) : -1;
                    if (ni < 0) ni = Math.max(0, Math.min(keepIdx, vis.length - 1));
                    recordFocus(cur, ni, (vis[ni] && typeof vis[ni].id === 'string') ? vis[ni].id : null);
                }
                savePrefs();
                parkWindow(win);                  // belongs to a hidden column now
                // The moved window is no longer visible, so it must not stay the
                // active/front window — drop frontId; reconcile may set a new one.
                if (frontId === win.id) frontId = null;
                applyWorkspaceView();
                requestRelayout();
                renderWorkspaces();
                applyTaskbarWorkspace();
                updateTaskbarActive();
                if (focusPrecap) {
                    requestAnimationFrame(() => reconcileTiledFocus(focusPrecap));
                }
            }
            // Send to a brand-new workspace (the title-bar menu's last entry).
            function sendWindowToNewWorkspace(win) {
                const st = wsStore();
                st.list.push({ id: mintWsUniqueId(), focusedCol: 0 });
                savePrefs();
                sendWindowToWorkspace(win, st.list.length - 1);
            }

                // ---- seams ------------------------------------------------
                // The one decision everything else hangs off: the strip draws
                // only the active workspace's columns.
                ctx.desktop.columnFilter(function (col, L) {
                    const st = (L.mods && L.mods.workspaces) || null;
                    if (!st || !Array.isArray(st.list) || !st.list.length) return true;
                    const id = col && col.wsId;
                    if (typeof id !== 'string') return true;   // unstamped -> adopted here
                    if (!st.list.some(function (w) { return w.id === id; })) return true;
                    return id === st.activeId;
                });
                // A column core just made belongs to the workspace you can see.
                ctx.desktop.onColumnCreated(function (col) {
                    if (col && typeof col.wsId !== 'string') col.wsId = activeWorkspaceId();
                });
                ctx.desktop.onPlaced(adoptFloatWorkspace);
                ctx.desktop.onForgotten(function (key) {
                    delete floatWsMap()[key];
                    savePrefsLocal();
                });
                // Invoking a window parked elsewhere must never no-op:
                //   tiled elsewhere -> go THERE (a tiled window's membership IS
                //                      its column, so it cannot be re-homed
                //                      without restructuring the layout)
                //   float elsewhere -> re-home it HERE (the #152 decision)
                //   masked but ours -> repair the stale mask
                // An all-workspaces float (windowWsId === null) is never
                // re-homed: it is not hidden by design.
                ctx.desktop.onReveal(function (win) {
                    if (win.tiled) {
                        const loc = findKeyInLayout(win.id);
                        if (loc && visibleColIndex(loc.col) === -1) {
                            const wi = workspaceIndexById(columnWsId(loc.col));
                            if (wi >= 0) switchWorkspace(wi);
                        }
                    } else if (windowOffActiveWs(win)) {
                        setWindowWs(win, activeWorkspaceId(), true);   // re-home + render
                    } else if (win.dom.classList.contains('ws-hidden')) {
                        applyWorkspaceVisibility();                    // repair the mask
                    }
                });
                // Repaint on every relayout: a /state adopt, the active-view
                // rebuild, a layout undo and the float<->tile switch all
                // converge there. All three calls are idempotent.
                ctx.desktop.onLayoutRender(function () {
                    adoptUnstampedColumns();
                    syncFocusedCol();
                    renderWorkspaces();
                    applyWorkspaceVisibility();
                    applyTaskbarWorkspace();
                });
                // ...and on every taskbar rebuild, which re-creates the chips
                // the ws badges hang off.
                ctx.taskbar.onItemsRendered(function () {
                    applyWorkspaceVisibility();
                    applyTaskbarWorkspace();
                });
                // A chip for a window on another workspace switches there first,
                // whether or not that window is currently open.
                ctx.taskbar.interceptActivate(function (key) {
                    const targetWs = workspaceIndexForKey(key);
                    if (targetWs === null || targetWs < 0
                        || targetWs === activeWorkspaceIndex()) return false;
                    switchWorkspace(targetWs);
                    return true;
                });
                // Title-bar menu: "Send to <workspace>" for a tiled window, the
                // all-workspaces toggle for a floating one.
                ctx.registerWindowMenuItems(function (win) {
                    if (!win) return [];
                    if (!win.tiled) {
                        // Workspace membership (task 8): locked to this ws, or
                        // shown on all. windowWsId null = all workspaces.
                        return [{
                            label: (windowWsId(win) === null)
                                ? '\u2713 On all workspaces'
                                : 'Show on all workspaces',
                            enabled: true,
                            action: () => setWindowAllWorkspaces(win, windowWsId(win) !== null),
                        }];
                    }
                    const items = [{ sep: true }];
                    const here = activeWorkspaceIndex();
                    wsList().forEach((ws, wi) => {
                        if (wi === here) return;
                        items.push({ label: 'Send to '
                                         + (ws.name ? ws.name : 'workspace ' + wsNumber(ws.id)),
                                     enabled: true,
                                     action: () => sendWindowToWorkspace(win, wi) });
                    });
                    items.push({ label: 'Send to new workspace', enabled: true,
                                 action: () => sendWindowToNewWorkspace(win) });
                    return items;
                });
                // Empty-desktop / strip menu, in BOTH window modes (#162 taught
                // core to call this from the floating branch too). The one
                // pointer surface that is always reachable: right-clicking the
                // empty part of the TASKBAR opens this same menu, and the
                // taskbar is always on screen even when windows cover the
                // desktop -- which is what makes it a safe home for the
                // workspace management that used to live only on a pager dot.
                ctx.registerDesktopMenuItems(function (info) {
                    const tiling = !!(info && info.tiling);
                    const items = [];
                    const cur = activeWorkspaceIndex();
                    const curWs = wsList()[cur];
                    wsList().forEach((ws, wi) => {
                        // The count means whatever this mode actually shows: a
                        // workspace's tiled columns, or -- in floating mode,
                        // where enterFloatingMode has detached every live window
                        // out of the strip -- its floating windows, counted by
                        // the same helper the hover preview's "+ N floating"
                        // line uses, so the two surfaces never disagree.
                        const n = tiling ? workspaceColumns(ws.id).length
                                         : countFloatingOnWs(ws.id);
                        // By id, not the captured index: a context menu is a
                        // frozen snapshot that can sit open across a poll, and a
                        // peer removing a LOWER-indexed workspace would slide
                        // this row's target onto its neighbour. The pager dots
                        // can capture an index because renderWsDots rebuilds
                        // them whenever the list changes; a menu never does.
                        const wsId = ws.id;
                        items.push({
                            label: (wi === cur ? '\u2713 ' : '   ')
                                + (ws.name ? ws.name : 'Workspace ' + wsNumber(ws.id))
                                + ' (' + n + ')',
                            enabled: true,
                            action: () => {
                                const i = workspaceIndexById(wsId);
                                if (i >= 0) switchWorkspace(i);
                            },
                        });
                    });
                    items.push({ label: '   New workspace', enabled: true,
                                 action: addWorkspace });
                    // Rename / remove for the workspace this menu was BUILT on.
                    // #162: buildWorkspaceMenu (a pager dot's right-click) used
                    // to be the only caller of either, so with the pager hidden
                    // they would be unreachable -- and they are worth having
                    // here even with the pager showing.
                    //
                    // The label NAMES its target and the action re-resolves that
                    // workspace BY ID at click time: a poll or a shortcut can
                    // switch workspaces while this menu sits open, and an
                    // unqualified "the active workspace" would then rename or
                    // remove one the user never saw marked. A workspace removed
                    // out from under us resolves to -1 and does nothing.
                    if (curWs) {
                        const curId = curWs.id;
                        const what = curWs.name ? curWs.name : 'workspace ' + wsNumber(curWs.id);
                        items.push({ sep: true });
                        items.push({ label: '   Rename ' + what + '\u2026',
                                     enabled: true,
                                     action: () => {
                                         const i = workspaceIndexById(curId);
                                         if (i >= 0) renameWorkspace(i);
                                     } });
                        // Disabled on the last workspace -- presentation only;
                        // removeWorkspace re-checks the count after its confirm
                        // dialog, so a peer removing the other one meanwhile
                        // cannot leave us at zero.
                        items.push({ label: '   Remove ' + what,
                                     enabled: wsList().length > 1,
                                     action: () => {
                                         const i = workspaceIndexById(curId);
                                         if (i >= 0) removeWorkspace(i);
                                     } });
                    }
                    return items;
                });
                // The seven shortcuts (plus #258's two unbound moves). Their
                // ids are VERBATIM the pre-#148 ones:
                // user rebindings are stored by id, and DEFAULT_KEYBINDINGS (54)
                // still carries the same defaults, so nothing about a user's
                // keyboard changes when this becomes a mod.
                ctx.registerKeyActions([
                    { id: 'workspace-prev',  label: 'Previous workspace',
                      run: () => switchWorkspace(activeWorkspaceIndex() - 1) },
                    { id: 'workspace-next',  label: 'Next workspace',
                      run: () => switchWorkspace(activeWorkspaceIndex() + 1) },
                    { id: 'workspace-1',     label: 'Go to workspace 1',
                      run: () => switchWorkspace(0) },
                    { id: 'workspace-2',     label: 'Go to workspace 2',
                      run: () => switchWorkspace(1) },
                    { id: 'workspace-3',     label: 'Go to workspace 3',
                      run: () => switchWorkspace(2) },
                    { id: 'workspace-4',     label: 'Go to workspace 4',
                      run: () => switchWorkspace(3) },
                    { id: 'workspace-5',     label: 'Go to workspace 5',
                      run: () => switchWorkspace(4) },
                    // #258: NEW ids, unbound by default (no DEFAULT_KEYBINDINGS
                    // entry). They move the ACTIVE workspace -- inside its
                    // stack when it is in one.
                    { id: 'workspace-move-left', label: 'Move workspace left',
                      run: () => moveEntry('w:' + activeWorkspaceId(), -1) },
                    { id: 'workspace-move-right', label: 'Move workspace right',
                      run: () => moveEntry('w:' + activeWorkspaceId(), 1) },
                ]);

                // ---- MCP desktop commands (#244) --------------------------
                // Thin wrappers over the primitives above, dispatched by the
                // /control desktop relay through ctx.commands.execute (86h).
                // Command VALUES carry data or a {refusal}; the relay maps
                // refusals onto the wire's error codes (needs_confirm keeps
                // its window count, last_workspace refuses, bad_args is a
                // validation failure) -- execute()'s own four-word vocabulary
                // stays clean. Workspace ops are admin-only at the BROKER;
                // nothing here knows credentials. remove NEVER opens a
                // dialog (owner decision: force moves the windows to the
                // adjacent workspace, exactly as the UI dialog says).
                if (ctx.commands && typeof ctx.commands.register === 'function') {
                    ctx.commands.register('list', { run: function () {
                        const st = wsStore();
                        const items = wsList().map(function (w, i) {
                            return { index: i, id: w.id,
                                     name: (typeof w.name === 'string')
                                         ? w.name : null };
                        });
                        // colId -> wsId (healed: unstamped/dangling read as
                        // the active workspace, the same rule the filter
                        // applies) and the browser-local float map, both for
                        // the relay's get-snapshot decoration. Additive: a
                        // consumer that ignores them is unaffected.
                        const columnWs = {};
                        for (const col of getLayout().columns) {
                            columnWs[col.id] = columnWsId(col, st);
                        }
                        return { active: activeWorkspaceIndex(), items: items,
                                 columnWs: columnWs, floatWs: floatWsMap() };
                    } });
                    ctx.commands.register('add', { run: function (args) {
                        const a = (args && typeof args === 'object') ? args : {};
                        if (a.name !== undefined && a.name !== null) {
                            if (typeof a.name !== 'string' || !a.name.trim()
                                    || a.name.trim().length > 40) {
                                return { refusal: 'bad_args' };
                            }
                        }
                        const entry = addWorkspaceNamed(
                            (typeof a.name === 'string') ? a.name : null);
                        return { index: wsList().length - 1, id: entry.id };
                    } });
                    ctx.commands.register('rename', { run: function (args) {
                        const a = (args && typeof args === 'object') ? args : {};
                        if (!Number.isInteger(a.index) || a.index < 0
                                || a.index >= wsList().length
                                || typeof a.name !== 'string') {
                            return { refusal: 'bad_args' };
                        }
                        const victim = wsList()[a.index];
                        if (!victim) return { refusal: 'unknown_window' };
                        return renameWorkspaceById(victim.id, a.name)
                            ? { renamed: true }
                            : { refusal: 'unknown_window' };
                    } });
                    ctx.commands.register('remove', { run: function (args) {
                        const a = (args && typeof args === 'object') ? args : {};
                        if (!Number.isInteger(a.index) || a.index < 0
                                || a.index >= wsList().length) {
                            return { refusal: 'bad_args' };
                        }
                        if (wsList().length <= 1) {
                            return { refusal: 'last_workspace' };
                        }
                        const victim = wsList()[a.index];
                        const count = wsLiveWindowCount(victim.id);
                        if (count > 0 && a.force !== true) {
                            return { refusal: 'needs_confirm', windows: count };
                        }
                        const r = removeWorkspaceById(victim.id);
                        if (!r) return { refusal: 'unknown_window' };
                        if (r.refused === 'last') {
                            return { refusal: 'last_workspace' };
                        }
                        return { removed: true, moved: count };
                    } });
                    ctx.commands.register('switch', { run: function (args) {
                        const a = (args && typeof args === 'object') ? args : {};
                        if (!Number.isInteger(a.index) || a.index < 0
                                || a.index >= wsList().length) {
                            return { refusal: 'bad_args' };
                        }
                        switchWorkspace(a.index);
                        return { active: a.index };
                    } });
                    ctx.commands.register('send-window', { run: function (args) {
                        const a = (args && typeof args === 'object') ? args : {};
                        const win = windows.get(a.key);
                        if (!win || win.disposed) {
                            return { refusal: 'unknown_window' };
                        }
                        if (!Number.isInteger(a.target) || a.target < 0
                                || a.target >= wsList().length) {
                            return { refusal: 'bad_args' };
                        }
                        sendWindowToWorkspace(win, a.target);
                        return { moved: true };
                    } });
                } else {
                    // Same-bundle ctx.commands is a guarantee (86h registers
                    // it unconditionally); reaching here means a broken
                    // build, and silence would read as "MCP workspaces are
                    // just absent" -- say so once, loudly.
                    try {
                        console.warn('[workspaces] ctx.commands missing -- '
                            + 'MCP desktop surface unavailable');
                    } catch (_) {}
                }

                // ---- teardown ---------------------------------------------
                // Dropping the filter is what un-hides everything (ctx's
                // disposer relayouts), so all that is left is clearing the
                // presentation echoes this mod applied: the float masks and the
                // taskbar badges/dimming. NOTHING in the store is touched --
                // re-enabling restores the same workspaces.
                ctx.onUnload(function () {
                    for (const win of windows.values()) {
                        if (win.disposed || !win.dom) continue;
                        if (!win.dom.classList.contains('ws-hidden')) continue;
                        win.dom.classList.remove('ws-hidden');
                        // .ws-hidden is display:none, so this window had a 0x0 box
                        // and sendResize bailed on it. Re-measure now it has one,
                        // else its PTY keeps whatever cols/rows it last saw.
                        refitSoon(win);
                    }
                    document.querySelectorAll('#taskbar-items .taskbar-item')
                        .forEach(function (el) {
                            el.classList.remove('other-ws');
                            el.classList.remove('ws-hidden');
                            const badge = el.querySelector('.ti-ws');
                            if (badge) badge.remove();
                        });
                    hideWsPreview();
                });

                adoptUnstampedColumns();
                renderWorkspaces();      // pager populated from the first paint
                applyWorkspaceVisibility();
            },
        });
