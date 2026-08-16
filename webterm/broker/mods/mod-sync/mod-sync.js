        // ---- mod: mod-sync (#158) -------------------------------------------
        // Copy this broker's mod setup to the OTHER brokers you have configured,
        // so the same choices aren't made by hand on every machine.
        //
        // The hard part is that "which mods are on" lives in three different
        // places, and only one of them is writable on a machine you are not
        // sitting at:
        //   * the per-browser enable set (localStorage 'webterm:mods:disabled')
        //     — deliberately per-browser (86_js_mod_loader.js:1328), and there is
        //     no wire that reaches another browser's localStorage at all;
        //   * a broker's mod POLICY pins (#157) — server-side, authoritative for
        //     every browser that loads that page, and the ONLY one of the three
        //     that is writable from here: POST /mods/policy is deliberately NOT
        //     lease-gated (app.py:4864) precisely so it can be set on a broker
        //     somebody else is using right now;
        //   * mod-owned settings — the /state settings blob, shared by every
        //     browser on that broker.
        // So PUSH writes the target's pins + its settings blob. It does not (and
        // cannot) reach into another browser's own choices, and every string in
        // the UI says so rather than implying a live remote reconfiguration.
        //
        // Model:
        //   push   - for each selected broker: merge this broker's mod-owned
        //            settings into its /state blob, then ONE POST /mods/policy
        //            carrying every pin change. Previewed per broker BEFORE
        //            anything is written, reported per broker after, with a
        //            per-row Retry and an Undo that restores the pins that
        //            broker had before the push.
        //   adopt  - the mirror, and named separately on purpose: it makes THIS
        //            BROWSER match what a broker you pick hands out. It reads
        //            that broker's pins + settings and applies them to this
        //            browser's own Mods choices (setModEnabled) — never to local
        //            pins, which would lock this browser's own checkboxes and,
        //            because pins resolve once at page load, would not even take
        //            effect until a reload.
        //   pins   - MINIMAL by default: a pin is written only where that
        //            broker's own default would land somewhere else, and a pin
        //            that is no longer needed is CLEARED. So syncing two brokers
        //            that already agree locks nothing. "Lock every mod" opts in
        //            to pinning all of them, which is the only way to also
        //            override a choice a browser over there made locally.
        //
        // Not in scope (deliberately, per #158): shipping mod CODE over the wire.
        // Nothing here transfers a .js/.css/mod.json; a broker can only be told
        // about mods it already serves, and a mod it does not serve is reported.
        //
        // Like every mod this shares the core page closure, so it calls getHosts(),
        // hostFetch(), fetchModCatalog(), saveModPins(), setModEnabled(),
        // savePrefs() etc. directly. All untrusted text (host labels, a peer's mod
        // titles, its setting values) is rendered with textContent, never innerHTML.
        registerMod({
            id: 'mod-sync',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['settings'],
            init: function (ctx) {
                const SELF = 'mod-sync';
                const MAX_TARGETS = 64;          // cap the fan-out, both ways
                const KEY_MAX = 128;             // a settings key we will carry
                const STR_MAX = 4096;            // a settings string value
                const DETAIL_MAX = 24;           // change lines shown per broker

                // ---- small helpers ------------------------------------------
                function label(h) {
                    return (h && (h.label || h.url || h.id)) || 'broker';
                }
                function el(tag, cls, text) {
                    const n = document.createElement(tag);
                    if (cls) n.className = cls;
                    if (text != null) n.textContent = text;
                    return n;
                }
                // A mod-owned setting we are willing to carry across brokers.
                // Every ctx.settings control today is a boolean/radio/select, so
                // every value is a scalar; refusing containers means an adopt can
                // never splice an arbitrary structure into the synced blob.
                function isScalar(v) {
                    return typeof v === 'boolean'
                        || (typeof v === 'number' && isFinite(v))
                        || (typeof v === 'string' && v.length <= STR_MAX);
                }
                // Peer-supplied text is bounded before it reaches the DOM. It is
                // only ever set with textContent, so this is about not letting a
                // hostile or merely enormous string blow the panel out — the same
                // treatment #157 gives a peer's mod titles (81:843).
                function cap(s, n) {
                    s = (s == null) ? '' : String(s);
                    return (s.length > n) ? (s.slice(0, n) + '…') : s;
                }
                function showValue(v) {
                    if (typeof v === 'string') return v === '' ? '(empty)' : cap(v, 120);
                    return cap(String(v), 120);
                }
                // A value an ACTIVE control will actually accept. isScalar bounds
                // the shape; this bounds the DOMAIN, which matters when adopting a
                // PEER's value: every mod validates read-through (an unknown value
                // silently falls back to its default WITHOUT rewriting the blob),
                // so planting an out-of-domain value would report a change that
                // never visibly happens and leave junk in the synced blob. A
                // radio/select's legal set is its own rendered options; a boolean's
                // is the two booleans. An unknown kind is left to isScalar alone.
                function acceptedBy(entry, v) {
                    if (!entry) return false;
                    if (entry.kind === 'boolean') return typeof v === 'boolean';
                    if (entry.kind === 'text') {
                        // #168: a text control has NO enumerable option set, so
                        // the DOM scrape below cannot answer for it and the bare
                        // `return true` at the bottom would plant a value read()
                        // then ignores -- exactly the failure this function
                        // exists to prevent. Its read-through gate is STRUCTURAL
                        // and deliberately domain-free: it honours a value this
                        // build's own validator would refuse (a zone the sending
                        // engine knows and ours does not is the case #168 fixes),
                        // so the honest answer here is that same gate, taken from
                        // the loader rather than re-implemented. Guarded like
                        // _localPin: a build without the predicate degrades to
                        // the scalar bound isScalar already applied.
                        try {
                            if (typeof _modTextOk === 'function') {
                                return _modTextOk(v, entry.maxLength);
                            }
                        } catch (_) { return false; }
                        return typeof v === 'string' && v.length <= STR_MAX;
                    }
                    if (entry.kind === 'select' || entry.kind === 'radio') {
                        if (typeof v !== 'string') return false;
                        let opts = [];
                        try {
                            opts = Array.from(entry.section.querySelectorAll(
                                'option, input[type="radio"]')).map((o) => o.value);
                        } catch (_) { return false; }
                        return opts.indexOf(v) !== -1;
                    }
                    return true;
                }
                // This broker's own verified identity, used to spot a "remote"
                // host entry that actually points back at us (an alias by IP,
                // say): pushing to it would write our OWN policy under another
                // broker's name. brokerId is learned from the real /info by
                // refreshHostIdentities, so it is the verified answer.
                function myBrokerId() {
                    try { return localHost().brokerId || ''; } catch (_) { return ''; }
                }

                // ---- what this broker's mod configuration IS ----------------
                // The per-mod choice in force HERE, for every mod this build
                // serves except ourselves. isModEnabled() is the resolved
                // PREFERENCE (a local pin, else this browser's toggle, else the
                // declared default) — not "is it running": a mod whose
                // dependency is off is still preferred-on while sitting inactive.
                // Preference is the right thing to copy; an init failure is a
                // local accident, not a configuration. Rows that are preferred-on
                // but blocked are called out in the preview rather than presented
                // as running.
                //
                // SELF is excluded unconditionally, in BOTH directions: adopting
                // a snapshot that turns mod-sync off would tear this mod down
                // mid-apply (86_js_mod_loader.js:1420), running its own unloads
                // while its dialog is open and orphaning the rest of the batch.
                function localMods() {
                    const out = [];
                    for (const m of window.__mods.registered) {
                        if (m.id === SELF) continue;
                        if (!MOD_ID_RE.test(m.id)) continue;
                        out.push({
                            id: m.id,
                            want: isModEnabled(m.id),
                            pinnedHere: _localPin(m.id) !== null,
                            requires: (m.requires || []).slice(),
                        });
                    }
                    return out;
                }
                // This broker's pin for one mod, or null. Guarded: _pin is
                // loader-private and newer than ctxVersion 1, so a build without
                // it degrades to "nothing is pinned" instead of throwing.
                function _localPin(id) {
                    try {
                        return (typeof _pin === 'function') ? _pin(id) : null;
                    } catch (_) { return null; }
                }
                // The mod-owned settings keys in force here: exactly the ones a
                // live ctx.settings control owns, so every key comes WITH its
                // owning mod id and no core or host-specific key (defaultHost,
                // defaultProfile, startPath, mcpModes, size, show, …) can be
                // swept in.
                //
                // KNOWN LIMIT, stated in help.md rather than hidden: a control
                // exists only while its mod is ACTIVE (86:596), so a mod that is
                // off here does not carry its settings. That is consistent — a
                // mod that is off here is being turned off there too, so its
                // settings do not apply — but it does mean a value a disabled mod
                // deliberately preserves (termfont keeps `termFont` so re-enabling
                // restores your font) stays behind. Turn the mod on, push, then
                // turn it off again if you want that value to travel.
                function localSettings() {
                    const out = [];
                    const seen = new Set();
                    for (const t of window.__mods.settingToggles) {
                        if (!t || t.kind === 'pane' || !t.key) continue;
                        if (typeof t.key !== 'string' || t.key.length > KEY_MAX) continue;
                        if (t.modId === SELF || seen.has(t.key)) continue;
                        let v;
                        try { v = t.read(); } catch (_) { continue; }
                        if (!isScalar(v)) continue;
                        seen.add(t.key);
                        out.push({ key: t.key, modId: t.modId, value: v });
                    }
                    return out;
                }

                // ---- targets -------------------------------------------------
                // Every configured REMOTE broker. The local broker is not a push
                // target: this broker is the SOURCE, and #157's own per-host Mods
                // section is where local pins are edited one at a time.
                //
                // A host is unusable as a target when we hold no password for it,
                // or its poll says the one we hold was refused — shown, never
                // selectable, so "why is that machine missing" is answerable.
                // Duplicates are collapsed by verified brokerId (else normalized
                // url) so two aliases for one broker are not pushed to twice
                // through two independent save chains.
                function targets() {
                    const mine = myBrokerId();
                    const out = [];
                    const seen = new Set();
                    for (const h of getHosts()) {
                        if (h.id === 'local') continue;
                        if (out.length >= MAX_TARGETS) break;
                        const key = (h.brokerId ? 'b:' + h.brokerId
                            : 'u:' + (normalizeHostUrl(h.url) || h.id));
                        if (seen.has(key)) continue;
                        seen.add(key);
                        let locked = '';
                        // Self-targeting must fail CLOSED. brokerId is the verified
                        // identity but is only known once that host has been polled,
                        // so an unpolled alias would slip through on that test alone
                        // and we would write THIS broker's own policy under a peer's
                        // name. The origin comparison catches the common alias with
                        // no identity yet.
                        const sameOrigin = (normalizeHostUrl(h.url) || '')
                            === window.location.origin;
                        if (sameOrigin || (mine && h.brokerId && h.brokerId === mine)) {
                            locked = 'this is this broker under another name';
                        } else if (!h.token) {
                            locked = 'no password saved';
                        } else {
                            let st = null;
                            try { st = pollStateFor(h.id); } catch (_) {}
                            if (st && st.authNeeded) locked = 'password refused';
                        }
                        out.push({ host: h, locked: locked });
                    }
                    return out;
                }

                // ---- the plan for ONE target ---------------------------------
                // Read that broker's served mods, master switch and existing pins
                // (GET /info via the shared fetchModCatalog, so this and #157's
                // pane cannot disagree about a peer), plus its raw /state blob for
                // the settings diff. Everything is decided here and shown before a
                // single byte is written.
                //
                // A broker we cannot enumerate is REFUSED, not pushed to blind:
                // without its catalog we cannot tell which of our keys it has any
                // use for, and we cannot pin at all. That covers an older build
                // ('unsupported'), a headless broker that serves no page and so no
                // mods, and the unreachable / unauthorized cases.
                async function planFor(host, lockAll) {
                    const plan = { hostId: host.id, name: label(host), state: '',
                                   why: '', modRows: [], setRows: [], setObj: {},
                                   priorPolicy: {}, pinsBlocked: '', extra: 0,
                                   // #191: does this target enforce the admin
                                   // credential class on POST /mods/policy?
                                   adminRequired: false,
                                   // Recorded so a Retry repeats THIS operation
                                   // rather than silently downgrading a locking
                                   // push to a minimal one.
                                   lockAll: !!lockAll };
                    await fetchModCatalog(host);
                    const rec = modCatalogCache.get(host.id);
                    if (!rec) { plan.state = 'unreachable'; return plan; }
                    plan.state = rec.state;
                    if (rec.state !== 'ok') {
                        plan.why = {
                            headless: 'serves no desktop page, so it serves no '
                                + 'mods — nothing here applies to it',
                            unsupported: 'older build: it does not report its '
                                + 'mods, so its mod setup cannot be read or '
                                + 'changed from here. Update it first.',
                            unauthorized: 'refused our password — set it on the '
                                + 'Browser tab',
                            unreachable: 'could not be reached',
                        }[rec.state] || 'its mods are unavailable';
                        return plan;
                    }
                    plan.priorPolicy = Object.assign({}, rec.policy);
                    // #191: decided per target, from the /info-derived record
                    // fetchModCatalog just refreshed, before anything is
                    // written -- see adminEnforcing for the rules.
                    plan.adminRequired = adminEnforcing(rec);
                    // A peer's catalog is UNTRUSTED input. Keep one sanitized array
                    // and use it everywhere, including for modPolicyImplied — handed
                    // the raw array, a single null/!object element throws on `m.id`
                    // and takes the whole preview down with no message.
                    const cat = (Array.isArray(rec.mods) ? rec.mods : []).filter(
                        (m) => m && typeof m === 'object'
                            && typeof m.id === 'string' && MOD_ID_RE.test(m.id));
                    const byId = new Map();
                    for (const m of cat) byId.set(m.id, m);
                    const mods = localMods();
                    const wantById = new Map(mods.map((m) => [m.id, m.want]));
                    // Mods that broker serves and we do not: we have no opinion
                    // about them and never touch them. Counted so the preview can
                    // say the match is over the mods we share, not everything.
                    for (const id of byId.keys()) {
                        if (id !== SELF && !wantById.has(id)) plan.extra += 1;
                    }
                    // --- pins ---
                    // Start from that broker's live policy and compute the
                    // MINIMAL edit that makes a fresh browser there resolve to
                    // our choice, then repair the implied-pin interactions.
                    const after = Object.assign({}, rec.policy);
                    const writes = {};                  // id -> true|false|null
                    for (const m of mods) {
                        const cat = byId.get(m.id);
                        if (!cat) continue;             // it does not serve it
                        const cur = rec.policy[m.id];
                        const dflt = (cat.default_enabled !== false);
                        let pin;
                        if (lockAll) {
                            pin = m.want;               // always explicit
                        } else if (dflt === m.want) {
                            // Its own default already lands where we want, so no
                            // lock is needed — and an existing pin that says the
                            // same thing is redundant: CLEAR it rather than leave
                            // that broker's checkbox locked forever.
                            pin = null;
                        } else {
                            pin = m.want;
                        }
                        if (pin === null) {
                            if (typeof cur === 'boolean') writes[m.id] = null;
                            delete after[m.id];
                        } else {
                            if (cur !== pin) writes[m.id] = pin;
                            after[m.id] = pin;
                        }
                    }
                    // A pinned-ON mod implies its dependencies ON, transitively,
                    // and that is resolved on the target by ITS loader over ITS
                    // catalog (86_js_mod_loader.js _resolvePins; mirrored for
                    // display by modPolicyImplied, 81). So "its default already
                    // matches, leave it unpinned" is not always enough: a mod we
                    // want OFF can be dragged ON by a pinned-ON dependent —
                    // including a dependent that only exists on THAT build, which
                    // we would otherwise never look at. An EXPLICIT pin always
                    // beats an implied one, so where the implication contradicts
                    // our choice we write the explicit pin. Bounded fixpoint:
                    // adding an explicit OFF can un-imply a chain behind it.
                    // Bounded fixpoint: `implied` is recomputed per pass,
                    // so the bound must exceed the deepest requires-chain a peer
                    // can declare (the deepest in-repo today is one link). The
                    // loop breaks as soon as a pass changes nothing.
                    for (let pass = 0; pass < 8; pass++) {
                        let changed = false;
                        const implied = modPolicyImplied(cat, after);
                        for (const m of mods) {
                            if (!byId.has(m.id) || m.want) continue;
                            if (after[m.id] !== undefined) continue;
                            if (!implied[m.id]) continue;
                            writes[m.id] = false;
                            after[m.id] = false;
                            plan.pinsBlocked = plan.pinsBlocked
                                || ('pinned off explicitly because '
                                    + implied[m.id] + ' on that broker requires it');
                            changed = true;
                        }
                        if (!changed) break;
                    }
                    // A broker with its master switch off stores pins fine, but
                    // none of its mods run, and #157's own editor disables its
                    // selects while it is off (81:860) — so a pin pushed now
                    // would be both inert AND awkward to clear later. Write none,
                    // and say so on the rows rather than showing writes that the
                    // push will silently drop.
                    const canPin = rec.modsEnabled !== false;
                    if (!canPin) {
                        plan.pinsBlocked = 'its master mod switch is off '
                            + '(mods_enabled=false), so no pins were written';
                    }
                    const finalImplied = modPolicyImplied(cat, after);
                    for (const m of mods) {
                        const cat = byId.get(m.id);
                        if (!cat) {
                            plan.modRows.push({ id: m.id, want: m.want,
                                action: 'missing',
                                note: 'not installed on that broker' });
                            continue;
                        }
                        const w = canPin ? writes[m.id] : undefined;
                        let note = '';
                        // Preferred on, but something it needs will not be there.
                        // Faithful to our own state (the loader here allows the
                        // same shape) but worth saying out loud.
                        if (m.want) {
                            const miss = (Array.isArray(cat.requires)
                                ? cat.requires : []).filter(function (d) {
                                    if (!byId.has(d)) return true;
                                    if (after[d] === false) return true;
                                    if (after[d] === true) return false;
                                    if (finalImplied[d]) return false;
                                    const dc = byId.get(d);
                                    return (dc.default_enabled === false);
                                });
                            if (miss.length) {
                                // A peer declares its own `requires` strings and
                                // the broker does not shape-check them, so cap.
                                note = 'needs ' + cap(miss.slice(0, 8).join(', '), 200)
                                    + ' — it will not run there';
                            }
                        }
                        if (w === undefined) {
                            plan.modRows.push({ id: m.id, want: m.want,
                                action: (!canPin && writes[m.id] !== undefined)
                                    ? 'master-off'
                                    : ((after[m.id] === undefined)
                                        ? 'default' : 'pinned'),
                                note: note });
                        } else {
                            plan.setObj[m.id] = w;
                            plan.modRows.push({ id: m.id, want: m.want,
                                action: (w === null) ? 'unpin' : 'pin',
                                pin: w, note: note });
                        }
                    }
                    // --- settings ---
                    // Only keys owned by a mod that broker actually serves; the
                    // rest have nothing to configure over there.
                    //
                    // If its /state cannot be read (it answered /info, so this is
                    // a transient or partial failure) the settings half is
                    // UNAVAILABLE, not empty: treating a failed read as "no
                    // current value" would preview every key as `(unset) → x` and
                    // present a blind whole-blob write as a clean diff. The pins
                    // still go — they ride a different endpoint.
                    const live = await readState(host);
                    if (!live) {
                        plan.settingsUnreadable = true;
                        return plan;
                    }
                    for (const s of localSettings()) {
                        if (!byId.has(s.modId)) {
                            plan.setRows.push({ key: s.key, modId: s.modId,
                                value: s.value, action: 'missing' });
                            continue;
                        }
                        const cur = live.settings[s.key];
                        plan.setRows.push({ key: s.key, modId: s.modId,
                            value: s.value, cur: cur,
                            action: (cur === s.value) ? 'same' : 'write' });
                    }
                    return plan;
                }

                // ---- the target's /state, read and written by key ------------
                // Deliberately NOT fetchHostState/putHostState. Two reasons, both
                // load-bearing for a BULK push:
                //   1. putHostState's 409 rebase adopts the winner's rev+layout
                //      but re-PUTs the WHOLE settings object it was handed
                //      (53_js_remote_host_cache.js:108) — correct for a settings
                //      tab whose blob IS the user's edit, but here it would erase
                //      every key another browser changed in the meantime. That is
                //      exactly the blind overwrite #158 forbids. We instead re-read
                //      the live blob and re-apply only OUR keys onto it, so a
                //      concurrent editor's other keys survive.
                //   2. fetchHostState runs the peer's blob through THIS build's
                //      normalizeSettings in place (53:49) and we would then PUT
                //      that back — a mod-only push must not quietly reshape a
                //      peer's unrelated settings with our rules.
                // A PUT carries settings AND layout, so the layout rides along
                // untouched; a bare {} would wipe it, so a broker whose layout we
                // could not read is refused rather than clobbered (the same rule
                // putHostState applies).
                //
                // ONE deadline covering connect THROUGH body (hence timeoutMs: 0):
                // hostFetch's own deadline stops at the response HEADERS, so a peer
                // that answers and then stalls its body would hang this await
                // forever — and the push walks its targets sequentially, so one
                // such peer would silently wedge the whole fan-out. Same guard, and
                // the same reason, as fetchHostState (53_js_remote_host_cache.js).
                async function readState(host) {
                    let r, srv;
                    const ctrl = new AbortController();
                    const timer = setTimeout(function () { ctrl.abort(); },
                        FETCH_TIMEOUT_MS);
                    try {
                        r = await hostFetch(host, '/state', {
                            cache: 'no-store', signal: ctrl.signal, timeoutMs: 0 });
                        if (!r.ok) return null;
                        srv = await r.json();
                    } catch (_) { return null; }
                    finally { clearTimeout(timer); }
                    if (!srv || typeof srv !== 'object') return null;
                    const settings = (srv.settings && typeof srv.settings === 'object'
                        && !Array.isArray(srv.settings)) ? srv.settings : {};
                    const realLayout = (srv.layout && typeof srv.layout === 'object'
                        && !Array.isArray(srv.layout));
                    const rev = (typeof srv.rev === 'number') ? srv.rev : 0;
                    return { rev: rev, settings: settings,
                             layout: realLayout ? srv.layout : {},
                             layoutLoaded: realLayout || rev === 0 };
                }
                // Merge `writes` ([{key,value}]) into that broker's settings and
                // PUT. Re-reads the live blob on every attempt (including the 409
                // retry) and re-applies only our keys, so this is a per-key merge,
                // never a whole-blob overwrite. Resolves 'ok' | 'not_active' |
                // 'error' | 'no_layout'.
                async function writeSettings(host, writes) {
                    for (let attempt = 0; attempt < 2; attempt++) {
                        const live = await readState(host);
                        if (!live) return 'error';
                        if (!live.layoutLoaded) return 'no_layout';
                        for (const w of writes) live.settings[w.key] = w.value;
                        let r;
                        try {
                            r = await hostFetch(host, '/state', {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    baseRev: live.rev,
                                    settings: live.settings,
                                    layout: live.layout,
                                    clientId: CLIENT_ID,
                                }),
                            });
                        } catch (_) { return 'error'; }
                        if (r.ok) return 'ok';
                        if (r.status !== 409) return 'error';
                        let j = null;
                        try { j = await r.json(); } catch (_) { j = null; }
                        // The lease is a PERMANENT refusal — another browser owns
                        // that broker's /state and no retry will change it.
                        if (j && j.error === 'not_active') return 'not_active';
                    }
                    return 'error';
                }

                // ---- admin credential class (#191) ---------------------------
                // A broker with an `admin_token` configured 403s admin_required
                // on POST /mods/policy unless the write carries X-Webterm-Admin.
                // Everything in this section is that migration, and nothing
                // else changes: the settings half rides /state, which is not
                // admin-gated.
                //
                // Detection is per TARGET and from its /info ONLY, riding the
                // `admin` key fetchModCatalog already carries in the shared
                // catalog record (beside update/restart): an enforcing build
                // publishes it, an old or non-enforcing build simply lacks it,
                // and absence is the old-build signal, never an error (#157's
                // rule: a new key is invisible to old peers). NEVER a probe
                // POST -- the probe IS the write. The routes-aware predicate
                // is shared with the mods pane (_adminRequiredFor, 86b) so
                // the two cannot disagree about a peer; guarded like _pin
                // because it is newer than ctxVersion 1, degrading to bare
                // key-presence. A record from a build whose fetcher does not
                // carry `admin` degrades to "not enforcing" -- the safe
                // direction: the token is then simply not sent, and an
                // enforcing broker's 403 still renders as an admin refusal.
                const POLICY_ROUTE = '/mods/policy';
                function adminEnforcing(rec) {
                    const info = (rec && rec.admin
                        && typeof rec.admin === 'object') ? rec.admin : null;
                    if (!info) return false;
                    try {
                        if (typeof _adminRequiredFor === 'function') {
                            return !!_adminRequiredFor(info, POLICY_ROUTE);
                        }
                    } catch (_) { return false; }
                    return true;
                }
                // The ONE deliberate second writer of POST /mods/policy: the
                // shared saveModPins cannot carry a header, and the admin
                // credential travels ONLY in X-Webterm-Admin -- never
                // Authorization (the page token already rides that on every
                // hostFetch wire; two credential classes must not share one
                // slot) and never the query string. Wire-identical to
                // saveModPins otherwise: same {set} body, same PATCH semantics,
                // same repaint of the shared catalog cache from the
                // authoritative reply. Called ONLY for a target that advertised
                // `admin` AND with a token in hand for this push; every other
                // write -- every old-build target included -- still goes
                // through saveModPins, so that wire stays byte-identical.
                async function saveModPinsAdmin(host, set, adminToken) {
                    if (!host) return { ok: false, error: 'no_host' };
                    const ids = Object.keys(set || {});
                    if (!ids.length) return { ok: true };
                    if (ids.length > MAX_MOD_POLICY_KEYS) {
                        return { ok: false, error: 'too_many' };
                    }
                    let r;
                    try {
                        r = await hostFetch(host, POLICY_ROUTE, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-Webterm-Admin': adminToken,
                            },
                            body: JSON.stringify({ set: set }),
                            // A credential never follows a redirect: fetch
                            // keeps custom headers across a 30x, so a bounce
                            // could carry this token somewhere the operator
                            // never named.
                            redirect: 'error',
                        });
                    } catch (_) { return { ok: false, error: 'unreachable' }; }
                    if (!r.ok) {
                        let e = null;
                        try { e = await r.json(); } catch (_) { e = null; }
                        return { ok: false, error: (e && e.error)
                            ? e.error : ('HTTP ' + r.status) };
                    }
                    let j = null;
                    try { j = await r.json(); } catch (_) { j = null; }
                    const rec = modCatalogCache.get(host.id);
                    if (rec && j && j.ok) {
                        rec.policy = sanitizeModPolicy(j.policy);
                    }
                    return { ok: true };
                }
                // ONE prompt per push, covering every enforcing target in it,
                // and the token lives in the caller's locals for that push
                // alone: prefs SYNC across browsers (#153) and localStorage is
                // same-origin-readable by every mod, so the page has no safe
                // standing store for an admin credential -- the per-push
                // prompt is the only sanctioned shape (#191). Deliberate v1
                // shape for a fleet whose brokers hold DIFFERENT admin tokens:
                // the one entered token goes to every enforcing target, the
                // mismatched ones 403 and render as admin-refused, and the
                // user re-runs the push per fleet segment -- no per-target
                // prompt matrix. Returns the entered string ('' pushes on
                // without the token; enforcing targets then refuse their
                // policy half and say so), or null on cancel, which aborts
                // the whole operation.
                async function promptAdminToken(names) {
                    let input = null;
                    const res = await openDialog({
                        title: 'Admin token required',
                        body: function (c) {
                            c.appendChild(el('div', 'app-dialog-msg',
                                'An admin token is required for mod policy '
                                + 'changes on the ' + names.length + ' broker'
                                + (names.length === 1 ? '' : 's')
                                + ' listed below. The page password is not '
                                + 'enough for this — those brokers gate '
                                + 'administration behind a separate '
                                + 'credential (admin_token in their '
                                + 'broker_config.json).'));
                            // EVERY recipient, never truncated: what you type
                            // here is sent to each of these brokers, so a
                            // name that scrolled off the end of a summary
                            // line would be a broker receiving a credential
                            // you were never shown. A broker earns a place on
                            // this list by claiming, in its own /info, that
                            // it requires one.
                            const who = el('ul', 'app-dialog-list');
                            for (const n of names) {
                                who.appendChild(el('li', null, String(n)));
                            }
                            c.appendChild(who);
                            const row = el('div', 'set-row app-dialog-field');
                            row.appendChild(el('label', null, 'Admin token'));
                            input = document.createElement('input');
                            input.type = 'password';
                            input.autocomplete = 'off';
                            row.appendChild(input);
                            c.appendChild(row);
                            c.appendChild(el('div', 'set-hint',
                                'Asked once per push, used only for it, never '
                                + 'stored, and never sent to a broker that '
                                + 'does not require it. The one token goes '
                                + 'to every broker listed; one holding a '
                                + 'different token refuses its policy '
                                + 'changes in its result row. Leaving it '
                                + 'empty pushes anyway, and those brokers '
                                + 'refuse their policy half.'));
                            // openDialog only auto-focuses its own `fields`;
                            // this input is body-built (a password box, which
                            // fields cannot make), so take focus right after
                            // the dialog's own focus pass runs.
                            setTimeout(function () {
                                try { input.focus(); } catch (_) {}
                            }, 0);
                        },
                        buttons: [
                            { label: 'Continue', value: 'go', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'go') return null;
                    return input ? input.value : '';
                }

                // ---- push ----------------------------------------------------
                // Settings first, then pins, and the row reports the two halves
                // separately. Order matters less than it looks: a pin only takes
                // effect the next time a browser loads that page, so by the time
                // any pin is consulted the settings write has long since resolved.
                // A refused settings write does NOT cancel the pins — the lease is
                // exactly what makes a busy broker's /state unwritable, and that is
                // the broker an operator most needs to administer.
                async function pushTo(plan, adminToken) {
                    const host = hostById(plan.hostId);
                    // Re-resolved at write time, never captured: hostFetch(null, …)
                    // silently resolves against OUR OWN origin with no token, so a
                    // host removed while the dialog was open must never reach it.
                    if (!host) {
                        return { name: plan.name, ok: false,
                                 detail: 'no longer configured' };
                    }
                    // adminRequired (a boolean, never the token) rides the
                    // result so Retry/Undo know to ask again (#191).
                    const out = { name: label(host), hostId: host.id, ok: true,
                                  parts: [], priorPolicy: plan.priorPolicy,
                                  wrote: {}, lockAll: !!plan.lockAll,
                                  adminRequired: !!plan.adminRequired };
                    const writes = plan.setRows.filter((r) => r.action === 'write');
                    if (writes.length) {
                        const res = await writeSettings(host, writes);
                        if (res === 'ok') {
                            out.parts.push(writes.length + ' setting'
                                + (writes.length === 1 ? '' : 's') + ' updated');
                        } else {
                            out.ok = false;
                            out.parts.push({
                                not_active: 'settings refused — another browser is '
                                    + 'the active view there',
                                no_layout: 'settings skipped — its layout could not '
                                    + 'be read, and a save would have wiped it',
                                error: 'settings could not be saved',
                            }[res] || 'settings could not be saved');
                        }
                    }
                    const ids = Object.keys(plan.setObj);
                    if (ids.length) {
                        // Record the ATTEMPT before making it, so Undo is offered
                        // even when the answer never arrives: a POST that times out
                        // or drops its connection may still have committed on the
                        // peer, and "restore the pins it had before" is the right
                        // action either way — a no-op when the write never landed.
                        out.wrote = Object.assign({}, plan.setObj);
                        // #191: the admin variant fires ONLY when this target
                        // advertised the admin class AND a token was entered
                        // for this push; every other write -- every old-build
                        // target included -- rides the shared saveModPins.
                        const useAdmin = !!(plan.adminRequired && adminToken);
                        const res = useAdmin
                            ? await saveModPinsAdmin(host, plan.setObj,
                                adminToken)
                            : await saveModPins(host, plan.setObj,
                                { quiet: true });
                        if (res && res.ok) {
                            const pins = ids.filter((i) => plan.setObj[i] !== null);
                            const clears = ids.length - pins.length;
                            out.parts.push(pins.length + ' pinned'
                                + (clears ? ', ' + clears + ' unpinned' : ''));
                        } else if (res && res.error === 'admin_required') {
                            // An enforcing target's refusal is an AUTH outcome,
                            // named as such -- never dressed as a network
                            // failure (#191).
                            //
                            // And retract the recorded attempt: unlike a
                            // dropped connection, this answer PROVES nothing
                            // was written (the gate refuses before the broker
                            // reads a body or takes the lock). Leaving it in
                            // `wrote` would offer an Undo for a write that
                            // never happened, which could later stamp stale
                            // prior pins over somebody else's real change.
                            out.wrote = {};
                            out.ok = false;
                            out.parts.push('mod policy refused — that broker '
                                + 'requires its admin token ('
                                + (useAdmin
                                    ? 'the one entered was not accepted'
                                    : 'none was entered') + ')');
                        } else {
                            out.ok = false;
                            out.parts.push('mod policy could not be changed'
                                + ((res && res.error) ? ' (' + res.error + ')' : ''));
                        }
                    }
                    if (plan.pinsBlocked) out.parts.push(plan.pinsBlocked);
                    if (!writes.length && !ids.length) {
                        out.parts.push('already matched — nothing to write');
                    }
                    return out;
                }
                // Restore the pins a broker had before one push: re-assert every
                // pin we changed back to its prior value (null clears one we
                // added). Best-effort and honest about it — the policy endpoint
                // has no revisions, so this is "put back what we found", not a
                // transactional undo, and a pin somebody else changed since is
                // overwritten. Settings are NOT undone: their prior values were
                // that broker's, and re-asserting a whole blob is the overwrite
                // this mod exists to avoid.
                async function undoPins(result) {
                    const host = hostById(result.hostId);
                    if (!host) {
                        showNotice('that broker is no longer configured.');
                        return;
                    }
                    const set = {};
                    for (const id of Object.keys(result.wrote || {})) {
                        const prior = result.priorPolicy[id];
                        set[id] = (typeof prior === 'boolean') ? prior : null;
                    }
                    if (!Object.keys(set).length) {
                        showNotice('nothing to undo on ' + label(host) + '.');
                        return;
                    }
                    // #191: undoing a push that needed the admin credential
                    // needs it again -- the token was held only for that push,
                    // so ask again rather than silently 403ing.
                    let tok = '';
                    if (result.adminRequired) {
                        const t = await promptAdminToken([label(host)]);
                        if (t === null) return;
                        tok = t;
                    }
                    const res = (result.adminRequired && tok)
                        ? await saveModPinsAdmin(host, set, tok)
                        : await saveModPins(host, set, { quiet: true });
                    if (res && res.ok) {
                        showNotice('Restored the mod pins ' + label(host)
                            + ' had before the push. Its browsers pick that up '
                            + 'on their next page load.');
                    } else if (res && res.error === 'admin_required') {
                        showNotice('Could not restore the pins on '
                            + label(host) + ' — it requires its admin token '
                            + (tok ? 'and did not accept the one entered.'
                                : 'and none was entered.'),
                            { sticky: true, type: 'error' });
                    } else {
                        showNotice('Could not restore the pins on '
                            + label(host) + '.', { sticky: true, type: 'error' });
                    }
                }

                // ---- adopt ---------------------------------------------------
                // What a broker hands the NEXT browser that loads it: its pin if
                // it has one, else the declared default of the mod as IT ships it.
                // That is all a peer can honestly tell us — a choice some browser
                // over there made in its own localStorage is invisible from here,
                // which is exactly why push writes pins.
                function sourceWants(rec, byId, implied, id) {
                    const pin = rec.policy[id];
                    if (typeof pin === 'boolean') return pin;
                    if (implied[id]) return true;
                    const cat = byId.get(id);
                    return cat ? (cat.default_enabled !== false) : null;
                }
                async function adoptPlan(host) {
                    const plan = { name: label(host), state: '', why: '',
                                   modRows: [], setRows: [] };
                    await fetchModCatalog(host);
                    const rec = modCatalogCache.get(host.id);
                    if (!rec) { plan.state = 'unreachable'; return plan; }
                    plan.state = rec.state;
                    if (rec.state !== 'ok') {
                        plan.why = {
                            headless: 'serves no desktop page, so it has no mod '
                                + 'setup to adopt',
                            unsupported: 'older build: it does not report its '
                                + 'mods, so there is nothing to read',
                            unauthorized: 'refused our password — set it on the '
                                + 'Browser tab',
                            unreachable: 'could not be reached',
                        }[rec.state] || 'its mods are unavailable';
                        return plan;
                    }
                    if (!rec.modsEnabled) {
                        plan.state = 'master_off';
                        plan.why = 'its master mod switch is off, so it runs no '
                            + 'mods at all — there is no setup to adopt';
                        return plan;
                    }
                    // Sanitize the peer's catalog once and use only that, for the
                    // same reason as planFor: modPolicyImplied dereferences `m.id`
                    // on every element, so one null would throw and take the whole
                    // preview down with no message.
                    const cat = (Array.isArray(rec.mods) ? rec.mods : []).filter(
                        (m) => m && typeof m === 'object'
                            && typeof m.id === 'string' && MOD_ID_RE.test(m.id));
                    const byId = new Map();
                    for (const m of cat) byId.set(m.id, m);
                    const implied = modPolicyImplied(cat, rec.policy);
                    // Walking OUR OWN registered mods, never the peer's catalog,
                    // is what keeps #163's installed mods out of adopt: a mod
                    // this build has never loaded has no local id to switch, so
                    // an `x-` package only that broker has is not adoptable and
                    // is not silently half-adopted either. #158's stance is
                    // unchanged -- pins and settings travel, code never does.
                    // A mod we have and it does not is REPORTED, not dropped:
                    // an omitted row reads as agreement.
                    for (const m of localMods()) {
                        if (!byId.has(m.id)) {
                            plan.modRows.push({ id: m.id, action: 'missing',
                                note: 'not installed there' });
                            continue;
                        }
                        const want = sourceWants(rec, byId, implied, m.id);
                        if (typeof want !== 'boolean' || want === m.want) {
                            plan.modRows.push({ id: m.id, want: m.want,
                                action: 'same' });
                        } else if (m.pinnedHere) {
                            // A local pin outranks this browser's own choice, so
                            // setModEnabled would refuse the write (86:1447).
                            // Report it instead of counting it as applied.
                            plan.modRows.push({ id: m.id, want: want,
                                action: 'pinned-here',
                                note: 'this broker pins it — its own Mods section '
                                    + 'decides' });
                        } else {
                            plan.modRows.push({ id: m.id, want: want,
                                action: 'write' });
                        }
                    }
                    // A failed read is NOT "it has no settings": without it every
                    // key would silently drop out and the diff would claim this
                    // browser already matches. Say so instead.
                    const live = await readState(host);
                    if (!live) plan.settingsUnreadable = true;
                    const byKey = new Map();
                    for (const t of window.__mods.settingToggles) {
                        if (t && t.key) byKey.set(t.key, t);
                    }
                    for (const s of (live ? localSettings() : [])) {
                        if (!byId.has(s.modId)) continue;
                        const there = live.settings[s.key];
                        if (there === undefined) continue;
                        if (!isScalar(there)) continue;
                        // The peer's value must be one the owning control would
                        // actually take, or adopting it is a change we report and
                        // the mod then ignores.
                        if (!acceptedBy(byKey.get(s.key), there)) {
                            plan.setRows.push({ key: s.key, modId: s.modId,
                                value: there, cur: s.value, action: 'rejected' });
                            continue;
                        }
                        plan.setRows.push({ key: s.key, modId: s.modId,
                            value: there, cur: s.value,
                            action: (there === s.value) ? 'same' : 'write' });
                    }
                    return plan;
                }
                // Apply an adopt to THIS browser. Settings first, so a mod brought
                // up right after already inits reading the new value and its
                // control seeds `last` from it (86:656) — no double onChange. Then
                // the enable changes, then the SAME convergence pass a /state pull
                // uses (applyDisplaySettings -> applyThemeSettings ->
                // notifyModSettings), so an already-running mod repaints instead of
                // showing a value that is no longer in the blob.
                function applyAdopt(plan) {
                    const s = getSettings();
                    let sw = 0;
                    for (const r of plan.setRows) {
                        if (r.action !== 'write') continue;
                        s[r.key] = r.value;
                        sw += 1;
                    }
                    if (sw) savePrefs();
                    let mw = 0, refused = 0;
                    for (const r of plan.modRows) {
                        if (r.action !== 'write') continue;
                        const got = setModEnabled(r.id, r.want);
                        if (got === r.want) mw += 1; else refused += 1;
                    }
                    if (sw) { try { applyDisplaySettings(); } catch (_) {} }
                    return { settings: sw, mods: mw, refused: refused };
                }

                // ---- dialog plumbing -----------------------------------------
                function mkCheck(text, checked, disabled) {
                    const row = el('label', 'modsync-row set-check');
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.checked = !!checked;
                    if (disabled) {
                        cb.disabled = true;
                        row.classList.add('modsync-locked');
                    }
                    row.appendChild(cb);
                    row.appendChild(el('span', 'modsync-name', text));
                    return { row: row, cb: cb };
                }
                // The per-broker change list shown in the preview: raw pin writes
                // and clears, the mods left alone and why, the settings that
                // change, and what is out of scope on that machine.
                function planLines(plan) {
                    const lines = [];
                    for (const r of plan.modRows) {
                        if (r.action === 'pin') {
                            lines.push(['write', r.id + ' → pin '
                                + (r.pin ? 'on' : 'off')
                                + (r.note ? ' (' + r.note + ')' : '')]);
                        } else if (r.action === 'unpin') {
                            lines.push(['write', r.id + ' → unpin (its own default '
                                + 'already gives ' + (r.want ? 'on' : 'off') + ')'
                                + (r.note ? ' (' + r.note + ')' : '')]);
                        } else if (r.action === 'master-off') {
                            lines.push(['skip', r.id + ' — would need a pin, but '
                                + 'that broker’s master mod switch is off']);
                        } else if (r.action === 'missing' || r.note) {
                            lines.push(['skip', r.id + ' — ' + r.note]);
                        }
                    }
                    for (const r of plan.setRows) {
                        if (r.action === 'write') {
                            lines.push(['write', r.key + ': '
                                + (r.cur === undefined ? '(unset)' : showValue(r.cur))
                                + ' → ' + showValue(r.value)]);
                        } else if (r.action === 'missing') {
                            lines.push(['skip', r.key + ' — ' + r.modId
                                + ' is not installed there']);
                        }
                    }
                    if (plan.settingsUnreadable) {
                        lines.push(['skip', 'its current settings could not be '
                            + 'read, so no settings are written — only pins']);
                    }
                    return lines;
                }
                function appendLines(host, lines) {
                    const wrap = el('div', 'modsync-plan');
                    for (const [kind, text] of lines.slice(0, DETAIL_MAX)) {
                        wrap.appendChild(el('div',
                            'modsync-change modsync-' + kind, text));
                    }
                    if (lines.length > DETAIL_MAX) {
                        wrap.appendChild(el('div', 'modsync-change',
                            '…and ' + (lines.length - DETAIL_MAX) + ' more'));
                    }
                    host.appendChild(wrap);
                    return wrap;
                }

                // ---- push dialogs --------------------------------------------
                async function openPushDialog() {
                    const rows = targets();
                    if (!rows.length) {
                        showNotice('No other brokers are configured — add one on '
                            + 'the Browser tab first.');
                        return;
                    }
                    const refs = [];
                    let lockBox = null;
                    const mods = localMods();
                    const on = mods.filter((m) => m.want).length;
                    const keys = localSettings().length;
                    const res = await openDialog({
                        title: 'Push mods to other brokers',
                        body: function (c) {
                            c.appendChild(el('div', 'app-dialog-msg',
                                'Make the brokers you pick match this one: '
                                + on + ' of ' + mods.length + ' mods on, and '
                                + keys + ' mod setting'
                                + (keys === 1 ? '' : 's') + '. Each broker is '
                                + 'previewed before anything is written.'));
                            const list = el('div', 'modsync-list');
                            for (const t of rows) {
                                const text = label(t.host)
                                    + (t.locked ? ' — ' + t.locked : '');
                                const r = mkCheck(text, !t.locked, !!t.locked);
                                list.appendChild(r.row);
                                if (!t.locked) {
                                    refs.push({ host: t.host, cb: r.cb });
                                }
                            }
                            c.appendChild(list);
                            const lr = mkCheck('Lock every mod on the target', false);
                            lockBox = lr.cb;
                            c.appendChild(lr.row);
                            c.appendChild(el('div', 'set-hint',
                                'Off by default: a pin is written only where that '
                                + 'broker’s own default would land somewhere '
                                + 'else, and a pin it no longer needs is cleared. '
                                + 'Ticking this pins every shared mod explicitly, '
                                + 'which locks those checkboxes for every browser '
                                + 'on that broker — the only way to also '
                                + 'override a choice made locally over there.'));
                        },
                        buttons: [
                            { label: 'Preview…', value: 'go', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'go') return;
                    const picked = refs.filter((r) => r.cb.checked);
                    if (!picked.length) {
                        showNotice('No brokers selected.');
                        return;
                    }
                    await openPreviewDialog(picked.map((r) => r.host),
                        !!(lockBox && lockBox.checked));
                }
                async function openPreviewDialog(hosts, lockAll) {
                    // Resolve every host again by id: the pick above and this
                    // read are separated by however long the user took.
                    const plans = [];
                    for (const h of hosts) {
                        const live = hostById(h.id);
                        if (!live) continue;
                        plans.push(await planFor(live, lockAll));
                    }
                    if (!plans.length) {
                        showNotice('Those brokers are no longer configured.');
                        return;
                    }
                    const refs = [];
                    const res = await openDialog({
                        title: 'Push mods — preview',
                        body: function (c) {
                            c.appendChild(el('div', 'app-dialog-msg',
                                'Exactly what will be written to each broker. '
                                + 'Mod pins take effect the next time a browser '
                                + 'loads that broker’s page, not now; '
                                + 'settings apply to its browsers as they poll.'));
                            const list = el('div', 'modsync-list');
                            for (const p of plans) {
                                if (p.state !== 'ok') {
                                    const r = mkCheck(p.name + ' — ' + p.why,
                                        false, true);
                                    list.appendChild(r.row);
                                    continue;
                                }
                                const lines = planLines(p);
                                const writes = lines.filter(
                                    (l) => l[0] === 'write').length;
                                // The change list is a SIBLING of the label, not
                                // inside it: a <label> makes every click in its
                                // subtree toggle the checkbox, and 24 lines of
                                // detail would be 24 ways to deselect a broker
                                // by accident.
                                const box = el('div', 'modsync-target');
                                const r = mkCheck(p.name + ' — '
                                    + (writes ? writes + ' change'
                                        + (writes === 1 ? '' : 's')
                                        : 'already matches'), writes > 0, !writes);
                                box.appendChild(r.row);
                                appendLines(box, lines);
                                if (p.extra) {
                                    box.appendChild(el('div',
                                        'modsync-change modsync-skip',
                                        p.extra + ' mod'
                                        + (p.extra === 1 ? '' : 's')
                                        + ' only that broker has: left alone'));
                                }
                                list.appendChild(box);
                                if (writes) refs.push({ plan: p, cb: r.cb });
                            }
                            c.appendChild(list);
                        },
                        buttons: [
                            { label: 'Push', value: 'push', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'push') return;
                    const go = refs.filter((r) => r.cb.checked).map((r) => r.plan);
                    if (!go.length) {
                        showNotice('Nothing selected to push.');
                        return;
                    }
                    // #191: ONE prompt per push, and only when some selected
                    // target both advertised the admin class AND is getting a
                    // policy write (the settings half rides /state, which is
                    // not admin-gated). Cancelling the prompt cancels the
                    // whole push; deselecting the enforcing brokers instead
                    // pushes without it.
                    let adminToken = '';
                    const enforcing = go.filter((p) => p.adminRequired
                        && Object.keys(p.setObj).length);
                    if (enforcing.length) {
                        const t = await promptAdminToken(
                            enforcing.map((p) => p.name));
                        if (t === null) return;
                        adminToken = t;
                    }
                    const out = [];
                    for (const p of go) out.push(await pushTo(p, adminToken));
                    results = out;
                    renderResults();
                    const bad = out.filter((r) => !r.ok).length;
                    showNotice(bad
                        ? ('Pushed to ' + (out.length - bad) + ' of ' + out.length
                            + ' brokers — see Control Panel → Browser '
                            + '→ Sync mods for what failed.')
                        : ('Pushed to ' + out.length + ' broker'
                            + (out.length === 1 ? '' : 's') + '. Mod pins apply '
                            + 'the next time a browser loads those pages.'),
                        bad ? { sticky: true, type: 'error' } : undefined);
                }

                // ---- adopt dialogs -------------------------------------------
                async function openAdoptDialog() {
                    const rows = targets();
                    if (!rows.length) {
                        showNotice('No other brokers are configured — add one on '
                            + 'the Browser tab first.');
                        return;
                    }
                    let chosen = null;
                    const res = await openDialog({
                        title: 'Adopt a broker’s mod setup',
                        body: function (c) {
                            c.appendChild(el('div', 'app-dialog-msg',
                                'Make THIS browser match a broker you pick. It '
                                + 'reads what that broker hands every browser '
                                + 'that loads it — its mod pins, or the '
                                + 'default of each mod as it ships them — '
                                + 'plus its mod settings. A choice some browser '
                                + 'over there made only for itself is not '
                                + 'visible from here.'));
                            const list = el('div', 'modsync-list');
                            for (const t of rows) {
                                const row = el('label', 'modsync-row set-check');
                                const rb = document.createElement('input');
                                rb.type = 'radio';
                                rb.name = 'modsync-src';
                                if (t.locked) {
                                    rb.disabled = true;
                                    row.classList.add('modsync-locked');
                                }
                                rb.addEventListener('change', function () {
                                    if (rb.checked) chosen = t.host.id;
                                });
                                row.appendChild(rb);
                                row.appendChild(el('span', 'modsync-name',
                                    label(t.host)
                                    + (t.locked ? ' — ' + t.locked : '')));
                                list.appendChild(row);
                            }
                            c.appendChild(list);
                            c.appendChild(el('div', 'set-hint',
                                'This changes this browser’s own Mods '
                                + 'choices and this broker’s mod settings '
                                + '(which its other browsers share). It pins '
                                + 'nothing here.'));
                        },
                        buttons: [
                            { label: 'Preview…', value: 'go', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'go') return;
                    const host = chosen ? hostById(chosen) : null;
                    if (!host) {
                        showNotice('Pick a broker to adopt from.');
                        return;
                    }
                    const plan = await adoptPlan(host);
                    if (plan.state !== 'ok') {
                        showNotice(plan.name + ' — ' + plan.why,
                            { sticky: true, type: 'error' });
                        return;
                    }
                    // Two lists, concatenated writes-first. appendLines truncates
                    // at DETAIL_MAX from the FRONT, so interleaving them would
                    // let a long tail of "not installed there" skips push the
                    // changes this dialog exists to confirm out of view.
                    const writes = [];
                    const skips = [];
                    let missing = 0;
                    for (const r of plan.modRows) {
                        if (r.action === 'write') {
                            writes.push(['write', r.id + ' → '
                                + (r.want ? 'on' : 'off')]);
                        } else if (r.action === 'pinned-here') {
                            skips.push(['skip', r.id + ' — ' + r.note]);
                        } else if (r.action === 'missing') {
                            // Say it. Omitting the row makes a mod that broker
                            // has never heard of look like one it agrees about.
                            missing += 1;
                            skips.push(['skip', r.id + ' — ' + r.note]);
                        }
                    }
                    for (const r of plan.setRows) {
                        if (r.action === 'rejected') {
                            skips.push(['skip', r.key + ' — that broker\'s value ('
                                + showValue(r.value) + ') is not one '
                                + r.modId + ' accepts here, so it is left alone']);
                            continue;
                        }
                        if (r.action !== 'write') continue;
                        writes.push(['write', r.key + ': ' + showValue(r.cur)
                            + ' → ' + showValue(r.value)]);
                    }
                    if (plan.settingsUnreadable) {
                        skips.push(['skip', 'its mod settings could not be read, '
                            + 'so only the on/off state above is compared']);
                    }
                    const lines = writes.concat(skips);
                    if (!writes.length) {
                        // Nothing to preview, so the skip lines above are never
                        // shown — carry the "not installed there" count into the
                        // notice rather than reporting a bare clean match.
                        showNotice((plan.settingsUnreadable
                            ? ('This browser matches ' + plan.name + '’s mod '
                                + 'on/off state; its settings could not be read.')
                            : ('This browser already matches ' + plan.name + '.'))
                            + (missing
                                ? (' Left alone: ' + missing + ' mod'
                                    + (missing === 1 ? '' : 's')
                                    + ' not installed there.')
                                : ''));
                        return;
                    }
                    const ok = await openDialog({
                        title: 'Adopt from ' + plan.name,
                        body: function (c) {
                            c.appendChild(el('div', 'app-dialog-msg',
                                'These changes apply to this browser now.'));
                            const box = el('div', 'modsync-list');
                            appendLines(box, lines);
                            c.appendChild(box);
                        },
                        buttons: [
                            { label: 'Adopt', value: 'apply', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!ok || ok.value !== 'apply') return;
                    const done = applyAdopt(plan);
                    showNotice('Adopted from ' + plan.name + ': '
                        + done.mods + ' mod'
                        + (done.mods === 1 ? '' : 's') + ' changed, '
                        + done.settings + ' setting'
                        + (done.settings === 1 ? '' : 's') + ' updated'
                        + (done.refused
                            ? ', ' + done.refused + ' refused (pinned here)' : '')
                        + '.');
                }

                // ---- Control Panel pane --------------------------------------
                // Results live here, not in a toast: a partial push across several
                // brokers has to stay readable afterwards, with a retry and an
                // undo per broker.
                let results = [];
                let resultsEl = null;
                function renderResults() {
                    if (!resultsEl) return;
                    resultsEl.textContent = '';
                    if (!results.length) return;
                    for (const r of results) {
                        const row = el('div', 'modsync-result modsync-result-'
                            + (r.ok ? 'ok' : 'fail'));
                        row.appendChild(el('span', 'modsync-name', r.name));
                        if (r.hostId) {
                            const again = el('button', null, 'Retry');
                            again.type = 'button';
                            again.addEventListener('click', function () {
                                // Rebuild the plan from scratch against the live
                                // host: a captured plan could write a stale pin
                                // set to a broker whose url, token or mods have
                                // changed since the preview.
                                const h = hostById(r.hostId);
                                if (!h) { showNotice('no longer configured.'); return; }
                                // Retry the SAME operation: carry the lock-every-mod
                                // choice through. Rebuilding minimally instead would
                                // not be a retry — in minimal mode every pin whose
                                // target default already agrees is an explicit CLEAR,
                                // so a "retry" of a locking push would quietly undo
                                // the locks it had just asked for.
                                planFor(h, !!r.lockAll).then(async function (p) {
                                    if (p.state !== 'ok') {
                                        showNotice(p.name + ' — ' + p.why,
                                            { sticky: true, type: 'error' });
                                        return;
                                    }
                                    // #191: a Retry is a push of its own, so an
                                    // enforcing target with policy writes asks
                                    // again -- the original push's token was
                                    // deliberately kept nowhere to reuse.
                                    let tok = '';
                                    if (p.adminRequired
                                            && Object.keys(p.setObj).length) {
                                        const t = await promptAdminToken(
                                            [p.name]);
                                        if (t === null) return;
                                        tok = t;
                                    }
                                    const out = await pushTo(p, tok);
                                    // Undo must still reach back to how that
                                    // broker looked before the FIRST push in
                                    // this row: keep the earliest prior value
                                    // for every id (a later Object.assign
                                    // argument wins, so the older map goes
                                    // last), and undo the union of what both
                                    // attempts wrote.
                                    out.priorPolicy = Object.assign({},
                                        out.priorPolicy, r.priorPolicy);
                                    out.wrote = Object.assign({},
                                        r.wrote, out.wrote);
                                    results = results.map(
                                        (x) => (x.hostId === r.hostId) ? out : x);
                                    renderResults();
                                }).catch(function () {
                                    showNotice('retry failed.',
                                        { type: 'error' });
                                });
                            });
                            row.appendChild(again);
                            if (Object.keys(r.wrote || {}).length) {
                                const un = el('button', null, 'Undo pins');
                                un.type = 'button';
                                un.title = 'restore the mod pins this broker had '
                                    + 'before the push';
                                un.addEventListener('click', function () {
                                    undoPins(r);
                                });
                                row.appendChild(un);
                            }
                        }
                        row.appendChild(el('div', 'modsync-detail',
                            (r.parts || [r.detail]).join('; ')));
                        resultsEl.appendChild(row);
                    }
                }
                function mkBtn(text, title, onClick) {
                    const b = el('button', null, text);
                    b.type = 'button';
                    if (title) b.title = title;
                    b.addEventListener('click', function (e) {
                        e.preventDefault();
                        if (typeof isAppDialogOpen === 'function'
                                && isAppDialogOpen()) return;
                        onClick();
                    });
                    return b;
                }
                function renderPane() {
                    const wrap = el('div');
                    wrap.appendChild(el('div', 'set-hint',
                        'Copy this broker’s mod setup to the other brokers '
                        + 'you have configured, instead of making the same '
                        + 'choices again on every machine. Push writes each '
                        + 'target’s own mod policy and mod settings — '
                        + 'it never sends mod code, and a broker that does not '
                        + 'have a mod is reported rather than silently skipped.'));
                    const row = el('div', 'set-row modsync-actions');
                    row.appendChild(mkBtn('Push to brokers…',
                        'make selected brokers match this one', openPushDialog));
                    row.appendChild(mkBtn('Adopt from a broker…',
                        'make this browser match a broker you pick',
                        openAdoptDialog));
                    wrap.appendChild(row);
                    resultsEl = el('div', 'modsync-results');
                    wrap.appendChild(resultsEl);
                    renderResults();
                    return wrap;
                }
                ctx.registerSettingsPane({
                    id: 'mod-sync',
                    mount: 'browser',
                    title: 'Sync mods',
                    render: renderPane,
                    // No reflect: the pane is action buttons plus the last push's
                    // result rows. Every dialog reads live getHosts() / the peer's
                    // /info + /state when it opens, so there is no cached state
                    // here for a /state pull to converge.
                });
            },
        });
