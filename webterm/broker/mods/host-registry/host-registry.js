        // ---- mod: host-registry (#65) -------------------------------------
        // An OPTIONAL shared broker list. Browserland's multi-host list lives in
        // this browser's localStorage (prefs._hosts) and is the source of truth;
        // opening the cockpit from another browser, or clearing local data, loses
        // it. This mod is a LAYER on top: it writes the list to a broker's server
        // store (ctx.serverStore -> /mod-store/host-registry) so another browser
        // can PULL it back. The broker only STORES the list — it never proxies;
        // every browser still connects DIRECTLY to each broker exactly as before.
        //
        // Model:
        //   publish  - write this browser's hosts to a broker's registry
        //   pull     - read a registry and merge per-entry, with a diff so a local
        //              url/token override is never silently clobbered
        //   publish-to-all - route the same snapshot to every configured broker
        //              (opts.host on ctx.serverStore), so opening from ANY of them
        //              recovers the list; per-broker success/failure is reported
        //   passwords - OFF by default; publishing tokens is a lateral-movement
        //              risk, so it is opt-in, loud, and revocable (Forget passwords
        //              republishes without tokens + purgeRevisions, clearing the
        //              server history — it does NOT revoke a token already pulled;
        //              rotate the broker token for real revocation)
        //
        // This is the first consumer of ctx.registerSettingsPane (a rich per-row
        // host list needs more than a boolean/select), mounted into the Browser
        // settings pane (mount:'browser') beside the Hosts list it manages. It
        // shares the core page closure like every mod, so it calls getHosts(),
        // savePrefs(), renderHostsList() etc. directly. All text is rendered with
        // textContent — labels/urls/tokens are untrusted and never innerHTML'd.
        registerMod({
            id: 'host-registry',
            version: '1.0.0',
            ctxVersion: 1,
            tiers: ['storage', 'settings'],
            init: function (ctx) {
                // Durable server storage is the whole point; no-op on an older
                // loader that predates ctx.serverStore (#124).
                if (!ctx.serverStore) return;

                const REG_MAX_HOSTS = 64;      // cap registry entries (both ways)
                const LABEL_MAX = 120;
                const TOKEN_MAX = 4096;
                const URL_MAX = 2048;
                const NOTIFIED_KEY = 'notifiedNonce';   // ctx.storage marker

                // ---- small helpers ------------------------------------------
                // Uniqueness, not secrecy: a nonce so a scrub/republish of the
                // SAME logical content is never deduped by the store, and so the
                // one-time notice can key off "a new publish happened".
                function makeNonce() {
                    return Date.now().toString(36) + '-'
                        + Math.random().toString(36).slice(2, 10);
                }
                // A loopback origin can't be reached by another machine, so the
                // publisher's own broker is skipped when its address is loopback.
                function isLoopbackOrigin(origin) {
                    try {
                        const h = new URL(origin).hostname;
                        return h === 'localhost' || h === '127.0.0.1'
                            || h === '::1' || h === '[::1]' || /^127\./.test(h);
                    } catch (_) { return false; }
                }
                function localOrigin() { return window.location.origin; }
                // Coerce an untrusted value to a bounded, control-char-free string.
                function cleanStr(v, max) {
                    if (typeof v !== 'string') return '';
                    return v.replace(/[\u0000-\u001f\u007f]/g, '').slice(0, max);
                }
                // strictHex (51_js_prefs.js) rejects a corrupt color to '' rather
                // than coercing it to blue like normalizeHex.
                function cleanColor(v) {
                    return (typeof v === 'string' && strictHex(v)) ? strictHex(v) : '';
                }

                // ---- publish: build the stored value ------------------------
                // selected: [{host, checked}]; localUrl: the editable "this broker"
                // address. Emits {v,updated,nonce,origin,hosts:[...]}. The local
                // host is published with an ABSOLUTE url (so other machines can
                // reach it) and its id is the broker_id (never the reserved
                // 'local', which is browser-local and would collide on pull).
                function buildValue(selected, includeTokens, localUrl) {
                    const hosts = [];
                    for (const sel of selected) {
                        if (!sel.checked) continue;
                        if (hosts.length >= REG_MAX_HOSTS) break;
                        const h = sel.host;
                        let e;
                        if (h.id === 'local') {
                            const origin = normalizeHostUrl(localUrl) || '';
                            // Can't share a loopback / missing address — skip it.
                            if (!origin || isLoopbackOrigin(origin)) continue;
                            const bId = cleanStr(h.brokerId, 128);
                            e = { id: bId, label: cleanStr(h.label, LABEL_MAX)
                                    || 'this broker', url: origin,
                                  color: cleanColor(h.color), hidden: false };
                            if (bId) e.brokerId = bId;
                        } else {
                            const origin = normalizeHostUrl(h.url);
                            if (!origin) continue;
                            const bId = cleanStr(h.brokerId, 128);
                            e = { id: cleanStr(h.id, 64),
                                  label: cleanStr(h.label, LABEL_MAX) || origin,
                                  url: origin, color: cleanColor(h.color),
                                  hidden: !!h.hidden };
                            if (bId) e.brokerId = bId;
                        }
                        if (includeTokens && typeof h.token === 'string' && h.token) {
                            e.token = cleanStr(h.token, TOKEN_MAX);
                        }
                        hosts.push(e);
                    }
                    return { v: 1, updated: Math.floor(Date.now() / 1000),
                             nonce: makeNonce(),
                             origin: cleanStr(localHost().brokerId, 128),
                             hosts: hosts };
                }

                // ---- pull: normalize + classify -----------------------------
                // One untrusted registry entry -> a clean shape, or null (dropped).
                // Every field is coerced + capped; the url is normalized to an
                // origin (normalizeHostUrl strips any path/query/fragment/userinfo
                // and rejects a non-http(s) scheme), so a hostile entry can't smuggle
                // a weird URL in.
                function normEntry(raw) {
                    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
                        return null;
                    }
                    const url = normalizeHostUrl(cleanStr(raw.url, URL_MAX));
                    if (!url) return null;                    // unusable -> drop
                    const e = { id: cleanStr(raw.id, 64),
                                label: cleanStr(raw.label, LABEL_MAX), url: url,
                                color: cleanColor(raw.color),
                                hidden: (raw.hidden === true),
                                brokerId: cleanStr(raw.brokerId, 128),
                                token: cleanStr(raw.token, TOKEN_MAX) };
                    if (!e.label) e.label = url;
                    return e;
                }
                // Find the LOCAL remote host an incoming entry refers to, or null.
                // Order: broker_id (the authoritative identity, verified locally via
                // /info) -> normalized url -> id. The local 'this broker' host is
                // never a match target (we never import over ourselves); an incoming
                // id of 'local' is reserved and ignored for id-matching.
                function matchLocal(e) {
                    const hosts = getHosts();
                    if (e.brokerId) {
                        for (const h of hosts) {
                            if (h.id !== 'local' && h.brokerId
                                    && h.brokerId === e.brokerId) return h;
                        }
                    }
                    for (const h of hosts) {
                        if (h.id !== 'local'
                                && normalizeHostUrl(h.url) === e.url) return h;
                    }
                    if (e.id && e.id !== 'local') {
                        for (const h of hosts) {
                            if (h.id !== 'local' && h.id === e.id) return h;
                        }
                    }
                    return null;
                }
                // A matched host "differs" on any user-visible field EXCEPT the
                // token: a token-less registry entry must not flag an otherwise
                // identical host, and the token is handled specially on apply.
                function hostDiffers(local, e) {
                    return normalizeHostUrl(local.url) !== e.url
                        || cleanStr(local.label, LABEL_MAX) !== e.label
                        || (local.color || '') !== e.color
                        || (!!local.hidden) !== (!!e.hidden);
                }
                // Classify every incoming entry vs the live local hosts. Drops this
                // broker (by origin OR broker_id) and de-dupes incoming by url.
                function classify(value) {
                    const myOrigin = localOrigin();
                    const myBrokerId = cleanStr(localHost().brokerId, 128);
                    const raw = (value && value.v === 1 && Array.isArray(value.hosts))
                        ? value.hosts : [];
                    const seenUrl = new Set();
                    const rows = [];
                    for (const r of raw) {
                        if (rows.length >= REG_MAX_HOSTS) break;
                        const e = normEntry(r);
                        if (!e) continue;
                        if (e.url === myOrigin) continue;               // us (url)
                        if (myBrokerId && e.brokerId === myBrokerId) continue; // us (id)
                        if (seenUrl.has(e.url)) continue;               // dupe url
                        seenUrl.add(e.url);
                        const local = matchLocal(e);
                        if (!local) {
                            rows.push({ kind: 'new', entry: e, local: null,
                                        checked: true });
                        } else if (!hostDiffers(local, e)) {
                            rows.push({ kind: 'identical', entry: e, local: local,
                                        checked: false });
                        } else {
                            rows.push({ kind: 'differs', entry: e, local: local,
                                        checked: false,
                                        urlChanged:
                                            normalizeHostUrl(local.url) !== e.url });
                        }
                    }
                    return rows;
                }

                // ---- per-host cache invalidation ----------------------------
                // When an apply changes a host's url (or token), drop every per-host
                // cache the app keeps keyed by host id, so the change takes effect
                // without a reload. This is a SUPERSET of what core's own edit path
                // (commitHostForm) clears today (only the first three) — a known
                // latent gap. Clearing a Map doesn't cancel an in-flight request,
                // but it drops the stale cache so the NEXT request uses the new
                // url/token and the poll loop re-primes on its next tick (the same
                // model core uses for a host edit). closeControlWs is idempotent
                // (a no-op when no socket is open).
                function invalidateHost(id) {
                    try { hostPolls.delete(id); } catch (_) {}
                    try { profilesCache.delete(id); } catch (_) {}
                    try { authPrompted.delete(id); } catch (_) {}
                    try { hostStateCache.delete(id); } catch (_) {}
                    try { hostSaveChains.delete(id); } catch (_) {}
                    try { mcpConfigCache.delete(id); } catch (_) {}
                    try { profilesConfigCache.delete(id); } catch (_) {}
                    try { closeControlWs(id); } catch (_) {}
                }

                // ---- apply (SYNCHRONOUS) ------------------------------------
                // All mutation happens here with NO awaits, so the poll loop can't
                // reassign prefs._hosts mid-batch: getHosts() is re-fetched once at
                // the top and every push/edit targets that one live array, then a
                // single savePrefs() + re-render commits it.
                function applyRows(rows) {
                    const hosts = getHosts();
                    let added = 0, updated = 0;
                    for (const row of rows) {
                        if (!row.checked || row.kind === 'identical') continue;
                        if (row.kind === 'new') {
                            if (hosts.length - 1 >= REG_MAX_HOSTS) continue;
                            // Mint a FRESH local id (never reuse the publisher's,
                            // which is their browser-local namespace) and DON'T
                            // import broker_id — refreshHostIdentities() learns it
                            // from the real /info, so we never trust an unverified
                            // imported identity. A re-pull still updates in place
                            // (matched by url/broker_id), so no duplicate.
                            hosts.push({ id: mintHostId(), label: row.entry.label,
                                         url: row.entry.url,
                                         token: row.entry.token || '',
                                         color: row.entry.color,
                                         hidden: row.entry.hidden });
                            added += 1;
                        } else if (row.kind === 'differs' && row.local) {
                            const local = row.local;
                            const urlChg =
                                normalizeHostUrl(local.url) !== row.entry.url;
                            if (urlChg) {
                                // The token travels WITH the url: on an origin
                                // change, NEVER keep the existing local token (it
                                // would be sent to the new origin). Adopt the
                                // imported token, or clear it so the user re-auths.
                                local.url = row.entry.url;
                                local.token = row.entry.token || '';
                                local.brokerId = row.entry.brokerId || '';
                            } else if (row.entry.token
                                    && row.entry.token !== local.token) {
                                // Same origin: a fresh token for the SAME broker is
                                // safe to adopt.
                                local.token = row.entry.token;
                            }
                            local.label = row.entry.label || local.label;
                            local.color = row.entry.color;
                            local.hidden = !!row.entry.hidden;
                            if (urlChg) invalidateHost(local.id);
                            updated += 1;
                        }
                    }
                    if (added || updated) {
                        // removeHost's fuller re-render sequence (the superset of
                        // the two core writers). renderHostStatus is called
                        // explicitly: refreshTaskbar coalesces on an in-flight run
                        // and can no-op. defaultHost (synced) is never touched.
                        savePrefs();
                        renderHostsList();
                        renderSettingsTabs();
                        renderHostStatus();
                        refreshTaskbar();
                    }
                    return { added: added, updated: updated };
                }

                // ---- publish flow -------------------------------------------
                // Write `value` to ONE broker: read its rev, then set() with a
                // one-shot conflict rebase (adopt the live rev, retry once). The
                // nonce makes every publish a real (non-deduped) write.
                async function publishTo(hid, value) {
                    const host = (hid === 'local') ? localHost() : hostById(hid);
                    const name = (host && host.id === 'local')
                        ? 'this broker' : (host ? host.label : hid);
                    let got = null;
                    try { got = await ctx.serverStore.get({ host: hid }); } catch (_) {}
                    const rev = (got && typeof got.rev === 'number') ? got.rev : 0;
                    let res = await ctx.serverStore.set(value, rev, { host: hid });
                    if (res && res.status === 409 && res.error === 'conflict'
                            && typeof res.rev === 'number') {
                        res = await ctx.serverStore.set(value, res.rev, { host: hid });
                    }
                    return { name: name, ok: !!(res && res.ok),
                             error: res && res.error };
                }
                async function doPublish(selected, includeTokens, localUrl, toAll) {
                    const value = buildValue(selected, includeTokens, localUrl);
                    if (!value.hosts.length) {
                        showNotice('Nothing to publish — no shareable hosts '
                            + 'selected (a loopback-only local address is skipped).');
                        return;
                    }
                    // Targets: the local broker always; every configured broker if
                    // "publish to all" was ticked. Each broker is an INDEPENDENT
                    // store, so results are reported per broker (never a blanket
                    // success when some failed).
                    const targets = toAll ? getHosts().map(h => h.id) : ['local'];
                    const results = [];
                    for (const hid of targets) {
                        results.push(await publishTo(hid, value));
                    }
                    const oks = results.filter(r => r.ok);
                    const fails = results.filter(r => !r.ok);
                    if (!fails.length) {
                        showNotice('Published to ' + oks.length + ' broker'
                            + (oks.length === 1 ? '' : 's')
                            + (includeTokens ? ', including passwords' : '') + '.',
                            includeTokens ? { sticky: true } : undefined);
                    } else {
                        const why = (f) => f.error === 'not_active'
                            ? 'another browser is active there'
                            : f.error === 'no_host' ? 'host not found'
                            : (f.error || 'failed');
                        showNotice('Published to ' + oks.length + ' of '
                            + results.length + ' brokers. Failed: '
                            + fails.map(f => f.name + ' (' + why(f) + ')').join('; '),
                            { sticky: true, type: 'error' });
                    }
                }

                // ---- forget passwords ---------------------------------------
                function stripTokens(value) {
                    const raw = (value && value.v === 1 && Array.isArray(value.hosts))
                        ? value.hosts : [];
                    return { v: 1, updated: Math.floor(Date.now() / 1000),
                             nonce: makeNonce(),
                             origin: (value && cleanStr(value.origin, 128)) || '',
                             hosts: raw.map(function (h) {
                                 const c = Object.assign({}, h);
                                 delete c.token;
                                 return c;
                             }) };
                }
                function valueHasTokens(value) {
                    const raw = (value && value.v === 1 && Array.isArray(value.hosts))
                        ? value.hosts : [];
                    return raw.some(h => h && typeof h.token === 'string' && h.token);
                }
                async function doForget() {
                    const ok = await openConfirmDialog({
                        title: 'Forget passwords',
                        message: 'Remove every password from this broker\'s '
                            + 'registry AND its saved history. This does not revoke '
                            + 'access: a browser that already pulled a password '
                            + 'keeps it, and a broker\'s token stays valid until you '
                            + 'rotate it. Continue?',
                        okLabel: 'Forget passwords', danger: true });
                    if (!ok) return;
                    let got = null;
                    try { got = await ctx.serverStore.get(); } catch (_) {}
                    if (!got || got.status === 0) {
                        showNotice('Could not read the registry on this broker.',
                            { sticky: true, type: 'error' });
                        return;
                    }
                    const rev = (typeof got.rev === 'number') ? got.rev : 0;
                    const stripped = stripTokens(got.value);
                    // purgeRevisions clears the ring so a token that scrolled into
                    // history is gone too. A fresh nonce makes the write non-deduped.
                    let res = await ctx.serverStore.set(stripped, rev,
                        { purgeRevisions: true });
                    if (res && res.status === 409 && res.error === 'conflict'
                            && typeof res.rev === 'number') {
                        res = await ctx.serverStore.set(stripped, res.rev,
                            { purgeRevisions: true });
                    }
                    if (!res || !res.ok) {
                        showNotice('Could not forget passwords: '
                            + (res && res.error === 'not_active'
                               ? 'another browser is active'
                               : (res && res.error) || 'failed'),
                            { sticky: true, type: 'error' });
                        return;
                    }
                    // Verify the purge took (an OLDER broker would ignore the flag
                    // and keep the ring): re-read and confirm no history + no
                    // tokens, else warn the user to rotate.
                    let after = null;
                    try { after = await ctx.serverStore.get(); } catch (_) {}
                    const cleared = after && Array.isArray(after.revisions)
                        && after.revisions.length === 0
                        && !valueHasTokens(after.value);
                    showNotice(cleared
                        ? 'Passwords removed from this broker\'s registry and history.'
                        : 'Passwords removed, but this broker kept revision history '
                          + '(older version?). Rotate the affected tokens to be safe.',
                        { sticky: true, type: cleared ? undefined : 'error' });
                }

                // ---- dialogs -------------------------------------------------
                function mkRowCheck(labelText, checked) {
                    const row = document.createElement('label');
                    row.className = 'hostreg-row set-check';
                    const cb = document.createElement('input');
                    cb.type = 'checkbox';
                    cb.checked = !!checked;
                    const span = document.createElement('span');
                    span.className = 'hostreg-name';
                    span.textContent = labelText;
                    row.appendChild(cb);
                    row.appendChild(span);
                    return { row: row, cb: cb, span: span };
                }
                async function openPublishDialog() {
                    const hosts = getHosts();
                    const refs = [];
                    let tokenBox = null, urlInput = null, allBox = null;
                    const res = await openDialog({
                        title: 'Publish broker list',
                        body: function (c) {
                            const intro = document.createElement('div');
                            intro.className = 'app-dialog-msg';
                            intro.textContent = 'Save this browser\'s host list to '
                                + 'the broker so another browser can pull it. The '
                                + 'broker only stores the list — it never proxies.';
                            c.appendChild(intro);
                            const list = document.createElement('div');
                            list.className = 'hostreg-list';
                            for (const h of hosts) {
                                const r = mkRowCheck(h.id === 'local'
                                    ? 'this broker' : (h.label + ' — ' + h.url),
                                    true);
                                list.appendChild(r.row);
                                refs.push({ host: h, cb: r.cb });
                            }
                            c.appendChild(list);
                            // Editable absolute address for THIS broker.
                            const urlRow = document.createElement('div');
                            urlRow.className = 'set-row';
                            const lab = document.createElement('label');
                            lab.textContent = 'This broker\'s address';
                            urlInput = document.createElement('input');
                            urlInput.type = 'text';
                            urlInput.value = localOrigin();
                            urlInput.placeholder = 'http://host:4445';
                            urlRow.appendChild(lab);
                            urlRow.appendChild(urlInput);
                            c.appendChild(urlRow);
                            const uh = document.createElement('div');
                            uh.className = 'set-hint';
                            uh.textContent = 'How other machines reach this broker. '
                                + 'A loopback address (localhost / 127.x) can\'t be '
                                + 'shared and is skipped.';
                            c.appendChild(uh);
                            // Include-passwords opt-in (OFF) + warning.
                            const tr = mkRowCheck('Include passwords (tokens)', false);
                            tr.row.classList.add('hostreg-danger');
                            tokenBox = tr.cb;
                            c.appendChild(tr.row);
                            const th = document.createElement('div');
                            th.className = 'set-hint hostreg-warn';
                            th.textContent = 'Off by default. A published token lets '
                                + 'anyone who can read this broker\'s registry log '
                                + 'into every included broker — a lateral-movement '
                                + 'risk. You can remove them later with Forget '
                                + 'passwords; rotate a token if it was exposed.';
                            c.appendChild(th);
                            // Publish-to-all opt-in.
                            const ar = mkRowCheck(
                                'Also publish to every configured broker', false);
                            allBox = ar.cb;
                            c.appendChild(ar.row);
                        },
                        buttons: [
                            { label: 'Publish', value: 'publish', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'publish') return;
                    await doPublish(
                        refs.map(r => ({ host: r.host, checked: r.cb.checked })),
                        !!(tokenBox && tokenBox.checked),
                        urlInput ? urlInput.value : localOrigin(),
                        !!(allBox && allBox.checked));
                }
                async function openPullDialog() {
                    let got = null;
                    try { got = await ctx.serverStore.get(); } catch (_) {}
                    // A non-200 GET means "no registry yet", not an error (an older
                    // broker still exposes ctx.serverStore); only a transport
                    // failure (status 0) is worth flagging.
                    if (got && got.status === 0) {
                        showNotice('Could not read the registry on this broker.',
                            { sticky: true, type: 'error' });
                        return;
                    }
                    const rows = classify(got && got.value);
                    if (!rows.length) {
                        showNotice('No hosts to import from this broker\'s registry.');
                        return;
                    }
                    const refs = [];
                    const res = await openDialog({
                        title: 'Pull broker list',
                        body: function (c) {
                            const intro = document.createElement('div');
                            intro.className = 'app-dialog-msg';
                            intro.textContent = 'Import hosts published to this '
                                + 'broker. New hosts are checked; hosts you already '
                                + 'have that differ are unchecked (your local copy '
                                + 'wins unless you tick them).';
                            c.appendChild(intro);
                            const list = document.createElement('div');
                            list.className = 'hostreg-list';
                            for (const row of rows) {
                                const tag = row.kind === 'new' ? 'new'
                                    : row.kind === 'differs' ? 'differs'
                                    : 'have';
                                const r = mkRowCheck('', row.checked);
                                r.row.classList.add('hostreg-' + row.kind);
                                if (row.kind === 'identical') r.cb.disabled = true;
                                const tagEl = document.createElement('span');
                                tagEl.className = 'hostreg-tag';
                                tagEl.textContent = tag;
                                r.span.textContent = row.entry.label + ' — '
                                    + row.entry.url;
                                r.row.insertBefore(tagEl, r.span);
                                if (row.entry.token) {
                                    const pw = document.createElement('span');
                                    pw.className = 'hostreg-haspw';
                                    pw.textContent = ' (has password)';
                                    r.span.appendChild(pw);
                                }
                                if (row.kind === 'differs' && row.urlChanged) {
                                    const w = document.createElement('div');
                                    w.className = 'set-hint hostreg-warn';
                                    w.textContent = 'URL differs — applying replaces '
                                        + 'your local one and clears its saved '
                                        + 'password (you may need to re-enter it).';
                                    r.row.appendChild(w);
                                }
                                list.appendChild(r.row);
                                refs.push({ row: row, cb: r.cb });
                            }
                            c.appendChild(list);
                        },
                        buttons: [
                            { label: 'Apply', value: 'apply', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'apply') return;
                    for (const r of refs) r.row.checked = r.cb.checked;
                    const out = applyRows(rows);
                    showNotice((out.added || out.updated)
                        ? ('Imported ' + out.added + ' new, updated ' + out.updated
                           + ' host' + (out.updated === 1 ? '' : 's') + '.')
                        : 'No changes applied.');
                }

                // ---- Control Panel pane -------------------------------------
                function mkBtn(label, title, onClick) {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.textContent = label;
                    if (title) b.title = title;
                    b.addEventListener('click', function (e) {
                        e.preventDefault();
                        // Guard against a double-open while a dialog is already up.
                        if (typeof isAppDialogOpen === 'function'
                                && isAppDialogOpen()) return;
                        onClick();
                    });
                    return b;
                }
                function renderPane() {
                    const wrap = document.createElement('div');
                    const desc = document.createElement('div');
                    desc.className = 'set-hint';
                    desc.textContent = 'Share this browser\'s broker list through '
                        + 'the broker so you can recover it in another browser or '
                        + 'after clearing local data. The list stays browser-local; '
                        + 'the broker only stores a copy and never proxies.';
                    wrap.appendChild(desc);
                    const row = document.createElement('div');
                    row.className = 'set-row hostreg-actions';
                    row.appendChild(mkBtn('Publish…',
                        'save this browser\'s host list to the broker',
                        openPublishDialog));
                    row.appendChild(mkBtn('Pull…',
                        'import a saved host list from the broker', openPullDialog));
                    row.appendChild(mkBtn('Forget passwords',
                        'remove tokens from the registry and its history', doForget));
                    wrap.appendChild(row);
                    return wrap;
                }
                ctx.registerSettingsPane({
                    id: 'host-registry',
                    mount: 'browser',
                    title: 'Broker registry',
                    render: renderPane,
                    // No reflect: the pane is action buttons only, and the dialogs
                    // read live getHosts()/the registry each time they open.
                });

                // ---- one-time discovery notice ------------------------------
                // On init, one GET of the LOCAL registry: if it holds hosts this
                // browser lacks, nudge the user toward Control Panel -> Browser.
                // Keyed by the registry NONCE (changes every publish, survives a
                // broker restart / rev reset), recorded at SHOW time (showNotice
                // has no dismiss callback), so it fires once per distinct publish.
                (async function () {
                    let got = null;
                    try { got = await ctx.serverStore.get(); } catch (_) {}
                    const value = got && got.value;
                    if (!value || value.v !== 1 || !Array.isArray(value.hosts)) return;
                    const nonce = (typeof value.nonce === 'string') ? value.nonce : '';
                    if (!nonce || ctx.storage.get(NOTIFIED_KEY) === nonce) return;
                    ctx.storage.set(NOTIFIED_KEY, nonce);   // record at show time
                    const newCount = classify(value)
                        .filter(r => r.kind === 'new').length;
                    if (!newCount) return;
                    showNotice('This broker has a shared host list with ' + newCount
                        + ' host' + (newCount === 1 ? '' : 's') + ' you don\'t have '
                        + 'yet — Control Panel → Browser → Broker registry → Pull.',
                        { sticky: true });
                })();
            },
        });
