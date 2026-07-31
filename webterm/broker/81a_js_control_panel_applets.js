        // ---- #181: the Control Panel as an applet grid ----------------------
        // The Control Panel used to be one vertical scroll: #set-pane-host held
        // seventeen static .set-section blocks plus everything mods mount into
        // #set-mods, under a broker tab bar, in a window at most 560x620 — so
        // reaching "Drag hold delay" meant scrolling past MCP tokens and the
        // launch-profile editor. The desktop generation this app already dresses
        // as solved that differently: a grid of captioned applet icons with a
        // status line under it, where you pick a topic first and get a small
        // focused dialog second. That is what this fragment builds.
        //
        // It is an IDIOM, not a clone. Nothing here names a vendor, a product or
        // a shipped applet: it is an early-90s desktop control panel — grey
        // chrome, chiselled bevels, an applet icon grid. The repo already set
        // that convention with the `redmond` theme's geographic codename.
        //
        // THREE THINGS THIS IS NOT, each of them a trap that was found while
        // scoping and is load-bearing here:
        //
        //   1. It is not a second renderer. Every .set-section still renders
        //      EAGERLY on tab switch exactly as before — renderModPolicy() is
        //      still painted synchronously before selectSettingsTab's await, and
        //      "applet opened" triggers no render. This code only decides what is
        //      VISIBLE. That is also why "Show everything" can be the same nodes
        //      with the applet class off rather than a parallel flat view: two
        //      renderers would drift the first time a section was added to one.
        //
        //   2. It is not a second inline-style writer. `.set-browser-global`
        //      sections already have TWO writers of inline style.display —
        //      applyBrowserGlobalVisibility() (81) and _controlSection() at
        //      create time (86) — and inline beats every selector, so a third
        //      writer means last-writer-wins and a remote broker's tab showing
        //      controls bound to the live LOCAL settings (#17, and #178's chip
        //      lesson restated). The applet router and the filter hide through
        //      CLASSES ONLY (.cp-applet-off / .cp-filter-off), which inline
        //      display:none still outranks — so a browser-global section hidden
        //      on a remote tab STAYS hidden whatever this code decides.
        //
        //   3. It does not own the panel's DOM. openControlPanelWindow BORROWS
        //      the singleton #settings-modal out of #settings-overlay and hands
        //      it back on close, so this fragment's nodes live INSIDE that node
        //      (see #cp-nav in 40_body.html) and NOTHING here may assume a
        //      reopen produces a fresh tree. Every listener is wired ONCE at page
        //      eval against the static markup, and cpResetControlPanelView()
        //      explicitly re-establishes the per-open state that survived.
        const CP_DEFAULT_APPLET = 'mods';
        // The applet map. `blurb` is the status line — one sentence saying what
        // the applet changes. That line is what makes an icon grid navigable
        // rather than a guessing game, so it is not optional polish; a tile with
        // no blurb would be a mystery box.
        const CP_APPLETS = [
            { id: 'desktop', title: 'Desktop',
              blurb: 'Changes how the desktop and its chrome look — colors, '
                   + 'background, and the buttons around the taskbar.' },
            { id: 'windows', title: 'Windows',
              blurb: 'Changes how windows are placed, sized, labelled and '
                   + 'moved.' },
            { id: 'input', title: 'Input',
              blurb: 'Changes keyboard shortcuts, and what programs on this '
                   + 'broker may do to your clipboard.' },
            { id: 'terminals', title: 'Terminals',
              blurb: 'Changes where new terminals start and which shell recipe '
                   + 'they launch.' },
            { id: 'startup', title: 'Startup',
              blurb: 'Changes what this browser does with your terminals when '
                   + 'the page reloads.' },
            { id: 'access', title: 'Access',
              blurb: 'Changes whether an external MCP server may read and drive '
                   + 'the terminals on this broker.' },
            { id: 'mods', title: 'Mods',
              blurb: 'Settings contributed by the mods you have enabled. A mod '
                   + 'may also place its control in one of the applets above.' },
            { id: 'advanced', title: 'Advanced',
              blurb: 'Changes what this broker pins for every browser that '
                   + 'loads its page.' },
        ];
        // Own-property index over a null-prototype bag. The keys are compared
        // against strings a MOD supplied (opts.mount) and against a data-
        // attribute, so an inherited 'constructor'/'toString' hit would make an
        // id nothing ever registered resolve as known (#163's lesson, applied to
        // a smaller bag).
        const CP_APPLET_INDEX = Object.create(null);
        for (const a of CP_APPLETS) CP_APPLET_INDEX[a.id] = a;
        // Core applets need their OWN icon table: APP_ICON_SVG (65) is keyed by
        // app KIND and is deliberately CLOSED, and "Windows" / "Input" /
        // "Startup" are not app kinds. Same house conventions as that table —
        // 24x24 viewBox, round caps/joins, aria-hidden, signature colors — and
        // like it, every string here is HARDCODED and trusted, so the one render
        // site below may innerHTML it. Nothing mod-supplied ever reaches this
        // table; see cpModBadge for the mod-owned path, which is text-only.
        const CP_APPLET_ICON_SVG = {
            'desktop':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="2.5" y="4" width="19" height="13" rx="1.5" fill="#cfe0f5" stroke="#3a5a8c" stroke-width="1.4"/>'
                + '<path d="M2.5 14h19" stroke="#3a5a8c" stroke-width="1.2"/>'
                + '<path d="M9 17l-.7 3h7.4L15 17z" fill="#8fa8c8" stroke="#3a5a8c" stroke-width="1.2" stroke-linejoin="round"/>'
                + '<circle cx="7" cy="8.5" r="1.6" fill="#f2b134"/></svg>',
            'windows':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="2.5" y="6.5" width="13" height="12" rx="1" fill="#e7eef8" stroke="#3a5a8c" stroke-width="1.4"/>'
                + '<path d="M2.5 10h13" stroke="#3a5a8c" stroke-width="1.2"/>'
                + '<rect x="8.5" y="2.5" width="13" height="12" rx="1" fill="#b9d0ee" stroke="#22508f" stroke-width="1.4"/>'
                + '<path d="M8.5 6h13" stroke="#22508f" stroke-width="1.2"/></svg>',
            'input':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="2" y="6.5" width="20" height="11" rx="1.5" fill="#e9e9ef" stroke="#4a4a60" stroke-width="1.4"/>'
                + '<path d="M5.5 10h1M9 10h1M12.5 10h1M16 10h1M19 10h.5M5.5 13h1M9 13h1M12.5 13h1M16 13h1M19 13h.5" stroke="#4a4a60" stroke-width="1.6" stroke-linecap="round"/>'
                + '<path d="M8 15.6h8" stroke="#7a7a94" stroke-width="1.8" stroke-linecap="round"/></svg>',
            'terminals':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="2.5" y="4" width="19" height="16" rx="1.5" fill="#1d2b22" stroke="#2f7d4f" stroke-width="1.4"/>'
                + '<path d="M6 9l3 2.5L6 14" fill="none" stroke="#59c98a" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
                + '<path d="M11.5 15h6" stroke="#59c98a" stroke-width="1.7" stroke-linecap="round"/></svg>',
            'startup':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M20 12a8 8 0 1 1-2.6-5.9" fill="none" stroke="#3d7bd6" stroke-width="2" stroke-linecap="round"/>'
                + '<path d="M20 3.5V8h-4.5" fill="none" stroke="#22508f" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
            'access':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<circle cx="8" cy="12" r="4.2" fill="none" stroke="#c9a227" stroke-width="2"/>'
                + '<path d="M12 12h9M18 12v3.5M15 12v2.5" fill="none" stroke="#c9a227" stroke-width="2" stroke-linecap="round"/></svg>',
            'mods':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<path d="M4 9h4V6.5a2 2 0 0 1 4 0V9h4v4h2.2a2 2 0 0 1 0 4H16v3H4z" fill="#8f7bd4" stroke="#4a3a86" stroke-width="1.3" stroke-linejoin="round"/></svg>',
            'advanced':
                '<svg viewBox="0 0 24 24" aria-hidden="true">'
                + '<rect x="3" y="3.5" width="18" height="6" rx="1" fill="#c3ccd8" stroke="#48566e" stroke-width="1.3"/>'
                + '<rect x="3" y="11.5" width="18" height="6" rx="1" fill="#c3ccd8" stroke="#48566e" stroke-width="1.3"/>'
                + '<circle cx="6.5" cy="6.5" r="1.1" fill="#2f7d4f"/>'
                + '<circle cx="6.5" cy="14.5" r="1.1" fill="#2f7d4f"/>'
                + '<path d="M10 6.5h8M10 14.5h8" stroke="#7a8699" stroke-width="1.4" stroke-linecap="round"/></svg>',
        };
        // OWN-property lookup only, so the return is ALWAYS either '' or one of
        // our own hardcoded strings — the render site can innerHTML it without a
        // type check. Exactly appIconSvg's contract, for a different table.
        function cpAppletIconSvg(id) {
            return Object.prototype.hasOwnProperty.call(CP_APPLET_ICON_SVG, id)
                ? CP_APPLET_ICON_SVG[id] : '';
        }
        // Resolve a placement HINT to an applet id. The set is CLOSED and owned
        // by core: an unrecognised string degrades to the Mods applet, it never
        // MINTS one. Auto-creating from the string would let a typo mint a
        // one-item applet titled by the typo, and 'Desktop ' with a trailing
        // space could sit beside the real one. Called by _controlSection (86),
        // whose historical contract for an unknown/omitted mount is already
        // "the shared bucket, so every existing control is unchanged".
        function cpAppletFor(hint) {
            return (typeof hint === 'string' && CP_APPLET_INDEX[hint])
                ? hint : CP_DEFAULT_APPLET;
        }
        // The default glyph for a mod that owns no app kind. Ours, not a mod's.
        const CP_MOD_BADGE_GLYPH = '◇';   // ◇
        // #181: a mod-owned section keeps its attribution when a `mount` hint
        // places it inside a CORE applet. With runtime installs (#163) an
        // installed mod is not first-party code, so "Color scheme" sitting in
        // Desktop must still read as mod-owned rather than as one of ours.
        //
        // Reuses the resolution chain help.js already implements rather than
        // inventing a second one: the trusted SVG if the mod owns an app kind,
        // else our own default glyph as TEXT. There is deliberately no
        // mod-supplied artwork path — a badge does not need to be distinctive
        // (the tooltip names the mod), and adding one would mean either a new
        // manifest field with a shipped/installed asymmetry or an HTML sink fed
        // by a string a mod may itself have taken off a store blob.
        function cpModBadge(modId) {
            const span = document.createElement('span');
            span.className = 'set-mod-badge';
            const svg = appIconSvg(modId);
            if (svg) span.innerHTML = svg;              // hardcoded table only
            else span.textContent = CP_MOD_BADGE_GLYPH;
            span.title = 'provided by the “' + modId + '” mod';
            return span;
        }

        // ---- view state -----------------------------------------------------
        // The applet the user navigated into, or null for the grid. Module-level
        // and NOT persisted: it is reset on every open (see
        // cpResetControlPanelView) because the borrowed singleton would
        // otherwise reopen wherever the last session left it.
        let cpOpenApplet = null;
        // "Show everything" — today's flat scroll. Browser-LOCAL view state, so
        // it lives on prefs directly. Two constraints decide the exact key:
        //   - OUTSIDE prefs._settings / prefs._layout, the only two things
        //     _stateBlob() serializes (52), so it never rides the synced blob
        //     and two people viewing one broker cannot fight over it.
        //   - UNDERSCORE-prefixed, because "Reset local view" (83) deletes every
        //     non-underscore top-level pref as per-session window geometry, and
        //     a bare `cpFlat` would be silently wiped by it.
        const CP_PREFS_KEY = '_controlPanel';
        function cpFlatMode() {
            const p = prefs[CP_PREFS_KEY];
            // === true, not truthy: a corrupted stored "false" must not enable it.
            return !!(p && typeof p === 'object' && p.flat === true);
        }
        function cpSetFlatMode(on) {
            let p = prefs[CP_PREFS_KEY];
            if (!p || typeof p !== 'object' || Array.isArray(p)) {
                p = {}; prefs[CP_PREFS_KEY] = p;
            }
            p.flat = (on === true);
            // savePrefsLocal, NOT savePrefs: this never belongs in /state, and
            // savePrefs would schedule a broker PUT of unrelated settings/layout
            // every time somebody toggled a view preference.
            savePrefsLocal();
        }
        function cpFilterQuery() {
            const el = document.getElementById('cp-filter');
            return el ? el.value.trim().toLowerCase() : '';
        }

        // Every .set-section the router governs. Deliberately a descendant query
        // rather than direct children: mod controls mount INSIDE #set-mods, and
        // that container is itself a .set-section (its exact markup is pinned by
        // a test) so it is skipped here and toggled separately below — it is
        // scaffolding, not a settings section, and it has no applet of its own.
        function cpMemberSections() {
            const pane = document.getElementById('set-pane-host');
            if (!pane) return [];
            const out = [];
            for (const el of pane.querySelectorAll('.set-section')) {
                if (el.id === 'set-mods') continue;
                out.push(el);
            }
            return out;
        }
        function cpAppletOf(el) {
            const a = (el.dataset && el.dataset.applet) || '';
            return CP_APPLET_INDEX[a] ? a : CP_DEFAULT_APPLET;
        }
        // Is this section available on the CURRENT tab at all? Reads the INLINE
        // display property and nothing else — never getComputedStyle, which our
        // own .cp-applet-off / .cp-filter-off classes would pollute into a
        // circular answer. That single read is automatically consistent with
        // BOTH inline writers (applyBrowserGlobalVisibility and _controlSection's
        // create-time write), including the awkward case they exist for: a mod
        // that mounts a section while a remote tab is open.
        function cpSectionAvailable(el) {
            return el.style.display !== 'none';
        }
        // The filter's haystack. Section titles, control labels and hints all
        // arrive via textContent; a <select>'s option labels come with it, which
        // is wanted (searching a theme name should find "Color scheme"). Field
        // VALUES and placeholders do not, so they are added explicitly — a start
        // path or a profile name you typed is exactly the thing you would search
        // for. Recomputed per call rather than cached: there are well under a
        // hundred sections, and a cache here buys nothing but invalidation bugs
        // (mods rebuild their own section DOM live, and renderers repaint rows
        // on every poll).
        function cpSectionHaystack(el) {
            let s = el.textContent || '';
            const fields = el.querySelectorAll('input, select, textarea');
            for (let i = 0; i < fields.length; i++) {
                const f = fields[i];
                if (f.type === 'checkbox' || f.type === 'radio') continue;
                s += ' ' + (f.value || '') + ' ' + (f.placeholder || '');
            }
            return s.toLowerCase();
        }

        // ---- the one visibility arbiter -------------------------------------
        // reconcileControlPanel() is the SOLE place applet/filter visibility is
        // decided. Everything that can change the answer calls it and nothing
        // else touches those classes: tab selection and renderSettings (through
        // applyBrowserGlobalVisibility, which is the one function that knows
        // browser-global visibility just moved), a mod mounting or removing a
        // section (_controlSection), the filter box, the flat toggle, a tile
        // click, Back, and panel open. Idempotent, cheap, and safe to call when
        // the panel is closed (the nodes are static markup that never leaves the
        // document).
        function reconcileControlPanel() {
            const pane = document.getElementById('set-pane-host');
            const nav = document.getElementById('cp-nav');
            if (!pane || !nav) return;
            const flat = cpFlatMode();
            const q = cpFilterQuery();
            const recs = [];
            const groups = Object.create(null);
            for (const a of CP_APPLETS) groups[a.id] = { avail: 0, hit: 0 };
            for (const el of cpMemberSections()) {
                const applet = cpAppletOf(el);
                const avail = cpSectionAvailable(el);
                const hit = !q || cpSectionHaystack(el).indexOf(q) !== -1;
                recs.push({ el: el, applet: applet, avail: avail, hit: hit });
                if (!avail) continue;
                const g = groups[applet];
                g.avail += 1;
                if (hit) g.hit += 1;
            }
            // An applet whose entire membership is unavailable on this tab is
            // not drawn, so it can never open onto blank space. That is the
            // all-browser-global case (Startup is one section, and it is
            // browser-global) and equally the case where a mod that owned an
            // applet's only section has just been disabled. If the OPEN applet
            // is the one that just emptied — a tab switch, a mod teardown — fall
            // back to the grid rather than showing a Back button over nothing.
            if (cpOpenApplet && !(groups[cpOpenApplet]
                    && groups[cpOpenApplet].avail)) {
                cpOpenApplet = null;
            }
            const open = flat ? null : cpOpenApplet;
            const mode = flat ? 'flat' : (open ? 'applet' : 'grid');
            for (const r of recs) {
                // In grid mode every section is off: the grid IS the content.
                const on = flat ? true : (open ? r.applet === open : false);
                r.el.classList.toggle('cp-applet-off', !on);
                r.el.classList.toggle('cp-filter-off', !r.hit);
                r.show = on && r.hit && r.avail;
            }
            // #set-mods is scaffolding with no applet, but it is a flex child of
            // a gapped column, so leaving it in the flow when all its children
            // are hidden would leave a stray gap. It carries no inline display
            // of its own (it is not .set-browser-global), so a class is safe.
            const modsHost = document.getElementById('set-mods');
            if (modsHost) {
                let any = false;
                for (const r of recs) {
                    if (r.show && r.el.parentNode === modsHost) { any = true; break; }
                }
                modsHost.classList.toggle('cp-applet-off', !any);
            }
            nav.classList.toggle('cp-mode-grid', mode === 'grid');
            nav.classList.toggle('cp-mode-applet', mode === 'applet');
            nav.classList.toggle('cp-mode-flat', mode === 'flat');
            const crumb = document.getElementById('cp-crumb');
            if (crumb) {
                crumb.textContent = (mode === 'applet' && CP_APPLET_INDEX[open])
                    ? CP_APPLET_INDEX[open].title
                    : (mode === 'flat' ? 'All settings' : 'Control Panel');
            }
            cpRenderAppletGrid(groups, q);
            cpSetStatus(null);
            // Hiding the section that holds focus would strand the caret on a
            // control that is no longer rendered (arrow keys and Tab then start
            // from nowhere). Move it somewhere real. Checked AFTER the classes
            // land so the test is what the user can actually see.
            const act = document.activeElement;
            if (act && act !== pane && pane.contains(act)
                    && act.closest('.cp-applet-off, .cp-filter-off')) {
                const filt = document.getElementById('cp-filter');
                if (filt) filt.focus();
                else if (act.blur) act.blur();
            }
        }
        // Repaint the tiles. Built from what is actually MOUNTED and available
        // right now, every time — a static applet table would go stale the first
        // time somebody toggled a mod, and the icon would advertise rows that
        // are not there. While a filter is active, an applet with no matching
        // section drops out too, which is the "typing hides applets with no hit"
        // half of the filter.
        function cpRenderAppletGrid(groups, q) {
            const grid = document.getElementById('cp-grid');
            if (!grid) return;
            grid.textContent = '';
            for (const a of CP_APPLETS) {
                const g = groups[a.id];
                if (!g || !g.avail) continue;
                if (q && !g.hit) continue;
                const tile = document.createElement('button');
                tile.type = 'button';
                tile.className = 'cp-tile';
                tile.dataset.applet = a.id;
                tile.title = a.blurb;
                const ic = document.createElement('span');
                ic.className = 'cp-tile-icon';
                const svg = cpAppletIconSvg(a.id);
                // Our own hardcoded markup, or nothing — never a mod's string.
                if (svg) ic.innerHTML = svg;
                const cap = document.createElement('span');
                cap.className = 'cp-tile-cap';
                cap.textContent = a.title;
                tile.appendChild(ic);
                tile.appendChild(cap);
                tile.addEventListener('click', () => cpOpenAppletView(a.id));
                // The status line is the point of the grid: hovering or tabbing
                // to a tile says what it changes, so picking one is a decision
                // rather than a guess.
                tile.addEventListener('mouseenter', () => cpSetStatus(a.blurb));
                tile.addEventListener('mouseleave', () => cpSetStatus(null));
                tile.addEventListener('focus', () => cpSetStatus(a.blurb));
                tile.addEventListener('blur', () => cpSetStatus(null));
                grid.appendChild(tile);
            }
        }
        // text === null restores the resting line for the current mode.
        function cpSetStatus(text) {
            const el = document.getElementById('cp-status');
            if (!el) return;
            if (typeof text === 'string') { el.textContent = text; return; }
            const grid = document.getElementById('cp-grid');
            el.textContent = (grid && grid.firstChild)
                ? 'Pick a topic — or type above to find a setting by name.'
                : 'No settings match. Clear the box above to see every topic.';
        }
        function cpOpenAppletView(id) {
            cpOpenApplet = CP_APPLET_INDEX[id] ? id : null;
            reconcileControlPanel();
            const back = document.getElementById('cp-back');
            if (cpOpenApplet && back) back.focus();
        }
        // Open whatever the filter has narrowed to, on a DELIBERATE gesture
        // (Enter in the box) rather than automatically as the count passes
        // through one. Auto-navigation was the obvious reading of "a single
        // surviving match opens straight to that applet", and it is wrong in
        // practice: typing "terminals" passes through "te", which may match one
        // section, so the view would jump mid-word — and the next keystroke,
        // matching none, would throw it back. Same for a backspace, a paste, an
        // IME composition, or a background repaint changing the count under the
        // user. Enter keeps the affordance and makes it something the user asked
        // for. Scrolls the match into view AFTER reconcile, so the section (and
        // its #set-mods ancestor) is actually rendered when we scroll to it.
        function cpOpenFilterMatch() {
            const q = cpFilterQuery();
            if (!q) return;
            let target = null, applet = null, count = 0;
            for (const el of cpMemberSections()) {
                if (!cpSectionAvailable(el)) continue;
                if (cpSectionHaystack(el).indexOf(q) === -1) continue;
                count += 1;
                if (!target) { target = el; applet = cpAppletOf(el); }
                else if (cpAppletOf(el) !== applet) applet = null;   // ambiguous
            }
            if (!target || !applet) return;   // nothing, or spread over applets
            cpOpenApplet = applet;
            reconcileControlPanel();
            try { target.scrollIntoView({ block: 'nearest' }); } catch (_) {}
        }
        // Called from openControlPanelWindow. The panel BORROWS a singleton that
        // is never destroyed, so a reopen inherits the last session's applet,
        // filter text, classes, grid DOM and scroll position. Reset the view
        // state that should not persist (everything except the flat toggle,
        // which is a deliberate preference) and reconcile unconditionally —
        // "nothing looks like it changed" is not a reason to skip it, because
        // the tab may have moved while the panel was closed.
        function cpResetControlPanelView() {
            cpOpenApplet = null;
            const filt = document.getElementById('cp-filter');
            if (filt) filt.value = '';
            const flatBox = document.getElementById('cp-flat-toggle');
            if (flatBox) flatBox.checked = cpFlatMode();
            reconcileControlPanel();
            const modal = document.getElementById('settings-modal');
            if (modal) modal.scrollTop = 0;
        }

        // ---- wiring ---------------------------------------------------------
        // ONCE, at page eval, against the static markup — not per open. The
        // borrowed node survives every close, so per-open wiring would stack a
        // fresh listener on each of them.
        (function cpWireNav() {
            const filt = document.getElementById('cp-filter');
            if (filt) {
                filt.addEventListener('input', () => reconcileControlPanel());
                filt.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        cpOpenFilterMatch();
                    } else if (e.key === 'Escape' && filt.value) {
                        // Clear the box rather than letting Escape bubble to the
                        // window's own close/cancel handling — the user is
                        // cancelling a search, not the panel.
                        e.preventDefault();
                        e.stopPropagation();
                        filt.value = '';
                        reconcileControlPanel();
                    }
                });
            }
            const flatBox = document.getElementById('cp-flat-toggle');
            if (flatBox) {
                flatBox.checked = cpFlatMode();
                flatBox.addEventListener('change', () => {
                    cpSetFlatMode(flatBox.checked);
                    reconcileControlPanel();
                });
            }
            const back = document.getElementById('cp-back');
            if (back) {
                back.addEventListener('click', () => {
                    const was = cpOpenApplet;
                    cpOpenApplet = null;
                    reconcileControlPanel();
                    // Land the caret back on the tile that was open, so keyboard
                    // navigation in and out of an applet is symmetrical.
                    const grid = document.getElementById('cp-grid');
                    const tile = grid && was
                        ? grid.querySelector('.cp-tile[data-applet="'
                            + cssEscape(was) + '"]') : null;
                    if (tile) tile.focus();
                });
            }
        })();
