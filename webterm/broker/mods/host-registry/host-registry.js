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
        //   encrypt   - #175. The value the broker stores can be encrypted IN THIS
        //              BROWSER first (WebCrypto, passphrase-derived), so the
        //              modstore JSON on disk holds ciphertext. Three modes, and
        //              the default ('passwords only') is a no-op until a publish
        //              actually carries a password. See the encryption block below
        //              for the threat model — it is NOT protection from the broker.
        //
        // This is the first consumer of ctx.registerSettingsPane (a rich per-row
        // host list needs more than a boolean/select), mounted into the Browser
        // settings pane (mount:'browser') beside the Hosts list it manages. It
        // shares the core page closure like every mod, so it calls getHosts(),
        // savePrefs(), renderHostsList() etc. directly. All text is rendered with
        // textContent — labels/urls/tokens are untrusted and never innerHTML'd.
        registerMod({
            id: 'host-registry',
            version: '1.1.0',
            ctxVersion: 1,
            tiers: ['storage', 'settings'],
            init: function (ctx) {
                // Durable server storage is the whole point; no-op on an older
                // loader that predates ctx.serverStore (#124).
                if (!ctx.serverStore) return;

                // ---- pure model + crypto ------------------------------------
                // Everything from here down to "---- dialogs ----" is
                // declarations only — nothing runs at load, nothing touches the
                // DOM. tests/test_host_registry_crypto.py slices exactly that
                // range and executes it in node against a stub browser, so the
                // encryption tests run the code that SHIPS rather than a copy of
                // it. Keep this range side-effect free.
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
                // #174 gave it a second job — dropping the loopback rows of a
                // REMOTE registry, where they name the publisher's machine and
                // would otherwise import as a host pointing at MY local broker,
                // carrying somebody else's password. So the spellings matter:
                // a trailing-dot FQDN, 0.0.0.0, and the IPv4-mapped IPv6 forms
                // all reach loopback and all used to slip through. This is a
                // "means nothing from here" filter, not a security boundary — a
                // remote registry can still name any reachable origin, which is
                // inherent to importing somebody's host list at all.
                function isLoopbackOrigin(origin) {
                    try {
                        let h = new URL(origin).hostname.toLowerCase();
                        if (h.length > 1 && h.charAt(h.length - 1) === '.') {
                            h = h.slice(0, -1);          // "localhost." resolves
                        }
                        if (h.charAt(0) === '[' && h.charAt(h.length - 1) === ']') {
                            h = h.slice(1, -1);          // URL keeps IPv6 brackets
                        }
                        // An IPv4-mapped address is normalized by URL() to the
                        // HEX form (::ffff:127.0.0.1 -> ::ffff:7f00:1), so match
                        // that; the dotted spelling is kept too for an engine
                        // that leaves it alone.
                        return h === 'localhost' || h === '::1' || h === '0.0.0.0'
                            || h === '::' || h === '::ffff:0:0'
                            || /^127\./.test(h)
                            || /^::ffff:7f[0-9a-f]{2}:/.test(h)
                            || /^::ffff:127\./.test(h)
                            || h === '::ffff:0.0.0.0';
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

                // ---- encryption (#175) --------------------------------------
                // Encrypt the published value in the browser, so the broker only
                // ever stores ciphertext — the modstore JSON next to the state
                // file, its backups, and the 50-deep revision ring.
                //
                // THREAT MODEL, stated as plainly here as in help.md. This
                // protects the registry AT REST and from anyone who can READ the
                // store: that file, a backup or snapshot of it, the revision
                // ring, another user or admin of that machine, a second browser
                // that holds the broker token but not the passphrase. It does
                // NOT protect against a COMPROMISED OR HOSTILE BROKER, because
                // that broker serves the very JS doing the encrypting and could
                // ship a build that pockets the passphrase. Anyone reaching for
                // this to defend against the broker itself is holding it wrong.
                //
                // Against a WRITER (someone who can PUT the registry without
                // knowing the passphrase) it buys forgery resistance, and that
                // shaped the format: the ciphertext carries WHOLE host records —
                // id, url and token together — and an unlocked pull uses the
                // DECRYPTED array and nothing else. Merging an encrypted token
                // onto a plaintext url beside it (by index, or by id) would let a
                // writer repoint a live credential at a host of their choosing
                // while the tag still verified. What it cannot buy: a writer can
                // still delete the value, replay an older one, or replace it with
                // a plaintext registry of their own — encryption is not
                // availability and not freshness. The pull diff (every incoming
                // entry classified, and ticked by hand) stays the real control.
                const ENC_ALG = 'AES-GCM';
                const ENC_KDF = 'PBKDF2-SHA-256';
                const ENC_ITER = 600000;        // what we WRITE
                const ENC_ITER_MIN = 100000;    // …and the band we agree to READ.
                const ENC_ITER_MAX = 1000000;   // A stored registry is untrusted
                                                // input: an unbounded iteration
                                                // count is a CPU bill anyone who
                                                // can write the store could hand
                                                // this browser, and it is charged
                                                // BEFORE the tag is checked.
                const ENC_SALT_BYTES = 16;
                const ENC_IV_BYTES = 12;
                const ENC_TAG_BITS = 128;
                const ENC_B64_MAX = 2000000;    // per encoded field
                const ENC_PASS_MIN = 8;
                // Written on every encrypted publish and NEVER read back or
                // shown: a breadcrumb for whoever opens webterm_modstore.json by
                // hand. Every string this mod displays is built locally — a
                // human-readable field a writer controls is a phishing surface.
                const ENC_NOTE = 'encrypted by browserland host-registry: '
                    + 'AES-GCM + PBKDF2-SHA-256, decrypted in the browser';

                // crypto.subtle is SECURE-CONTEXT ONLY, and the broker terminates
                // no TLS — so http://<lan-ip>:4445, which is how most of these
                // run, has window.isSecureContext false and crypto.subtle
                // undefined. Same gate shape as canReadClipboard
                // (63_js_clipboard_auth.js). Read through window.* so the whole
                // gate is one stubbable surface.
                function encAvailable() {
                    return !!(window.isSecureContext && window.crypto
                        && window.crypto.subtle && window.crypto.getRandomValues);
                }
                function encWhyUnavailable() {
                    return 'this page is not a secure context ('
                        + localOrigin() + '), and crypto.subtle needs https:// '
                        + 'or localhost.';
                }

                // Strict base64. atob() tolerates a wrong-length tail and some
                // stray characters; we would rather call a malformed envelope
                // malformed than hand crypto.subtle something surprising.
                // -> Uint8Array, or null (never a throw) on anything unexpected.
                const ENC_B64_RE = /^[A-Za-z0-9+/]*={0,2}$/;
                function encB64Bytes(s) {
                    if (typeof s !== 'string' || !s || s.length > ENC_B64_MAX
                            || s.length % 4 !== 0 || !ENC_B64_RE.test(s)) {
                        return null;
                    }
                    let bin;
                    try { bin = atob(s); } catch (_) { return null; }
                    const out = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
                    return out;
                }
                function encBytesB64(bytes) {
                    let s = '';
                    const CHUNK = 0x8000;   // apply() has an argument-count limit
                    for (let i = 0; i < bytes.length; i += CHUNK) {
                        s += String.fromCharCode.apply(null,
                            bytes.subarray(i, i + CHUNK));
                    }
                    return btoa(s);
                }

                // Failure kinds, because they mean different things to the user:
                //   'envelope'   - malformed / unsupported. NOT a passphrase
                //                  problem, so never re-prompt on it.
                //   'passphrase' - AES-GCM authentication failed. A wrong key and
                //                  a modified ciphertext / iv / header are the
                //                  SAME failure and cannot be told apart, so the
                //                  message has to name both.
                //   'payload'    - decrypted, but not the shape we write.
                function encErr(kind, reason) {
                    const e = new Error(reason || kind);
                    e.encError = kind;
                    return e;
                }

                // Additional authenticated data: the envelope HEADER. Not secret
                // — bound. Without it a writer could edit `iter` (bill this
                // browser for a slow derive) or `scope` (change how a reader
                // treats the value) and still hand back a blob that
                // authenticates. Rebuilt from the DECLARED values on read, so any
                // edit to them fails the tag check.
                function encAad(iter, scope) {
                    return new TextEncoder().encode('browserland/host-registry/1|'
                        + ENC_ALG + '|' + ENC_KDF + '|' + iter + '|' + scope);
                }
                async function encDerive(pass, salt, iter) {
                    const base = await window.crypto.subtle.importKey('raw',
                        new TextEncoder().encode(pass), { name: 'PBKDF2' },
                        false, ['deriveKey']);
                    // Non-extractable: the page never gets the raw key back.
                    return await window.crypto.subtle.deriveKey(
                        { name: 'PBKDF2', salt: salt, iterations: iter,
                          hash: 'SHA-256' },
                        base, { name: ENC_ALG, length: 256 }, false,
                        ['encrypt', 'decrypt']);
                }
                // Seal a plain object. A FRESH salt AND iv every time — the key
                // is re-derived per publish rather than cached, so an iv can
                // never be reused under one key.
                async function encSeal(payload, pass, scope) {
                    const salt = window.crypto.getRandomValues(
                        new Uint8Array(ENC_SALT_BYTES));
                    const iv = window.crypto.getRandomValues(
                        new Uint8Array(ENC_IV_BYTES));
                    const key = await encDerive(pass, salt, ENC_ITER);
                    const ct = await window.crypto.subtle.encrypt(
                        { name: ENC_ALG, iv: iv, tagLength: ENC_TAG_BITS,
                          additionalData: encAad(ENC_ITER, scope) },
                        key, new TextEncoder().encode(JSON.stringify(payload)));
                    return { alg: ENC_ALG, kdf: ENC_KDF, iter: ENC_ITER,
                             scope: scope, salt: encBytesB64(salt),
                             iv: encBytesB64(iv),
                             ct: encBytesB64(new Uint8Array(ct)) };
                }
                // Structural validation, run BEFORE a passphrase is asked for, so
                // an unreadable envelope is reported as unreadable and the user
                // is never sent round a retype loop that cannot succeed. Returns
                // null when usable, else a short reason.
                function encProblem(e) {
                    if (!e || typeof e !== 'object' || Array.isArray(e)) {
                        return 'not an envelope';
                    }
                    if (e.alg !== ENC_ALG) return 'unsupported cipher';
                    if (e.kdf !== ENC_KDF) return 'unsupported key derivation';
                    if (typeof e.iter !== 'number' || !isFinite(e.iter)
                            || Math.floor(e.iter) !== e.iter
                            || e.iter < ENC_ITER_MIN || e.iter > ENC_ITER_MAX) {
                        return 'unsupported iteration count';
                    }
                    if (e.scope !== 'tokens' && e.scope !== 'all') {
                        return 'unknown scope';
                    }
                    const salt = encB64Bytes(e.salt);
                    const iv = encB64Bytes(e.iv);
                    const ct = encB64Bytes(e.ct);
                    if (!salt || salt.length !== ENC_SALT_BYTES) return 'bad salt';
                    if (!iv || iv.length !== ENC_IV_BYTES) return 'bad iv';
                    // At or below the tag length there is no ciphertext at all.
                    if (!ct || ct.length <= ENC_TAG_BITS / 8) {
                        return 'bad ciphertext';
                    }
                    return null;
                }
                async function encOpen(e, pass) {
                    const bad = encProblem(e);
                    if (bad) throw encErr('envelope', bad);
                    const key = await encDerive(pass, encB64Bytes(e.salt), e.iter);
                    let pt;
                    try {
                        pt = await window.crypto.subtle.decrypt(
                            { name: ENC_ALG, iv: encB64Bytes(e.iv),
                              tagLength: ENC_TAG_BITS,
                              additionalData: encAad(e.iter, e.scope) },
                            key, encB64Bytes(e.ct));
                    } catch (_) { throw encErr('passphrase'); }
                    let obj = null;
                    try { obj = JSON.parse(new TextDecoder().decode(pt)); }
                    catch (_) {}
                    if (!obj || typeof obj !== 'object' || Array.isArray(obj)
                            || !Array.isArray(obj.hosts)) {
                        throw encErr('payload', 'unexpected payload');
                    }
                    return obj;
                }

                // The passphrase lives in memory for this page's life and is
                // NEVER persisted — there is deliberately no "remember on this
                // browser", because a key sitting in localStorage is worth
                // exactly as much as the passphrase to anything that can read
                // localStorage. It is held so publish-to-all and back-to-back
                // pulls don't re-prompt, and dropped by Forget passphrase or the
                // mod's teardown. "Dropped" means the reference is released — a
                // JS string cannot be wiped.
                let _encPass = null;
                // Single-flight over every registry action. openDialog is a
                // SINGLETON that cancels whatever dialog is live, so a pull that
                // is mid-derive must not be able to have its retry dialog shot
                // down by a publish started in the meantime (or by a second click
                // on itself) — the button guard only covers "a dialog is open
                // right now", which an await between dialogs is not.
                let _encBusy = false;
                // Assigned once the Control Panel control exists (below). A `let`
                // seeded null, not a const declared later, because the pane's
                // reflect() runs during registerSettingsPane and would otherwise
                // read it in its temporal dead zone — which throws, and a throw in
                // init disables the whole mod.
                let _encSetting = null;
                function encMode() {
                    return _encSetting ? _encSetting.get() : 'off';
                }
                function encModeLabel(mode) {
                    return mode === 'all' ? 'Whole list'
                        : mode === 'tokens' ? 'Passwords only' : 'Off';
                }
                // 'tokens' | 'all' | 'unknown' (an envelope we can't read) | ''.
                function encScope(value) {
                    const e = (value && typeof value === 'object')
                        ? value.enc : null;
                    if (!e || typeof e !== 'object' || Array.isArray(e)) return '';
                    return (e.scope === 'tokens' || e.scope === 'all')
                        ? e.scope : 'unknown';
                }

                // ---- publish: build the stored value ------------------------
                // selected: [{host, checked}]; localUrl: the editable "this broker"
                // address. Emits {v,updated,nonce,origin,hosts:[...]}. The local
                // host is published with an ABSOLUTE url (so other machines can
                // reach it) and its id is the broker_id (never the reserved
                // 'local', which is browser-local and would collide on pull).
                // This is always the PLAINTEXT value; sealing (if any) happens
                // after, in sealTokens/sealAll.
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
                function valueHasPlainTokens(value) {
                    const raw = (value && Array.isArray(value.hosts))
                        ? value.hosts : [];
                    return raw.some(h => h && typeof h.token === 'string' && h.token);
                }

                // ---- publish: seal ------------------------------------------
                // 'tokens' KEEPS the v1 shape and its plaintext host list, minus
                // every token, and seals the WHOLE records (tokens included).
                // Staying at v1 is deliberate: an older build reads the plaintext
                // side and imports labels/urls exactly as if the list had been
                // published without Include passwords — strictly better than the
                // "no hosts to import" a v2 would leave it with — and it cannot
                // be talked into adopting a token, because there is no token on
                // that side to adopt. An unlocked pull in THIS build ignores the
                // plaintext side entirely (see unlockForPull). `hasToken` marks
                // the entries whose password is in the envelope, so the pull diff
                // can say "locked" without the passphrase.
                async function sealTokens(value, pass) {
                    const enc = await encSeal({ hosts: value.hosts }, pass,
                        'tokens');
                    const pub = value.hosts.map(function (h) {
                        const c = Object.assign({}, h);
                        if (typeof c.token === 'string' && c.token) {
                            c.hasToken = true;
                        }
                        delete c.token;
                        return c;
                    });
                    return { v: 1, updated: value.updated, nonce: value.nonce,
                             origin: value.origin, hosts: pub,
                             enc: enc, encNote: ENC_NOTE };
                }
                // 'all' is v2: nothing about the list survives outside the
                // envelope — not the labels, not the addresses, not even which
                // broker published it. `nonce` and `updated` stay outside because
                // the one-time discovery notice keys off the nonce and must work
                // without the passphrase.
                async function sealAll(value, pass) {
                    const enc = await encSeal(
                        { origin: value.origin, hosts: value.hosts }, pass, 'all');
                    return { v: 2, updated: value.updated, nonce: value.nonce,
                             origin: '', enc: enc, encNote: ENC_NOTE };
                }
                // What a publish has to do, as ONE decision: seal ('tokens' /
                // 'all' / '' = publish in the clear), or refuse outright. FAIL
                // CLOSED — "the setting says encrypt but this page can't" must
                // never quietly degrade to publishing the tokens in the clear,
                // which would undo the point of gating the option on the secure
                // context in the first place. mode 'tokens' with nothing to
                // encrypt is a genuine no-op: there is no secret in the value, so
                // it goes out as today's plain v1 with no prompt.
                function encPlan(mode, value, available) {
                    const wants = (mode === 'all') ? 'all'
                        : (mode === 'tokens' && valueHasPlainTokens(value))
                            ? 'tokens' : '';
                    if (!wants) return { seal: '', blocked: false };
                    if (!available) return { seal: '', blocked: true };
                    return { seal: wants, blocked: false };
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
                    // #175: a password we know about but may not hold — either
                    // it is right here, or the plaintext side of a 'tokens'
                    // registry says the envelope has one. Display only.
                    e.hasToken = !!e.token || (raw.hasToken === true);
                    if (!e.label) e.label = url;
                    return e;
                }
                // The hosts a registry value exposes WITHOUT the passphrase.
                // NEVER used once a payload has been decrypted — the decrypted
                // array is the authenticated one and is used alone. Any `token`
                // found here is DROPPED: the plaintext side of a 'tokens'
                // registry is written token-free, so a token in it is somebody
                // else's addition, and adopting an attacker-chosen token is how
                // you get talked into authenticating to their broker. A plain v1
                // registry (no envelope) is unchanged — its tokens are trusted
                // exactly as much as the broker storing them, which is the
                // pre-existing bargain Include passwords already spells out.
                function publicHostsOf(value) {
                    if (!value || typeof value !== 'object') return [];
                    const scope = encScope(value);
                    if (scope === 'all' || scope === 'unknown') return [];
                    if (value.v !== 1 || !Array.isArray(value.hosts)) return [];
                    if (!scope) return value.hosts;
                    return value.hosts.map(function (h) {
                        if (!h || typeof h !== 'object' || Array.isArray(h)) {
                            return h;
                        }
                        const c = Object.assign({}, h);
                        delete c.token;
                        return c;
                    });
                }
                // Find the LOCAL remote host an incoming entry refers to, or null.
                // Order: broker_id (the authoritative identity, verified locally
                // via /info) -> normalized url. The local 'this broker' host is
                // never a match target (we never import over ourselves).
                //
                // #174 removed a third fallback, matching the incoming `id`
                // against a local host id. It contradicted the rule two functions
                // down — a publisher's id is THEIR browser-local namespace, which
                // is why applyRows mints a fresh one — and it was reachable from a
                // registry somebody else wrote: publish once and your host ids are
                // in that broker's store, so a hostile copy could name one and
                // repoint the host it belongs to. url and broker_id cover the real
                // cases. The cost is narrow: a broker that moved address AND whose
                // broker_id this browser never learned now imports as a second
                // entry instead of updating in place.
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
                // …but an incoming PASSWORD that is not the one we hold IS a real
                // difference, and it used to be invisible: an otherwise identical
                // host classified as 'identical', whose checkbox is disabled, so a
                // rotated password could never be pulled back at all. #174 makes
                // that its own row kind, because recovering a password from
                // another broker's registry is half the point of reading one.
                // Still unchecked by default, like every other 'differs'.
                function tokenDiffers(local, e) {
                    return !!e.token && e.token !== (local.token || '');
                }
                // ---- merge N sources (#174) ---------------------------------
                // sources: [{name, local, hosts}] in a FIXED order (this broker
                // first, then the host list's own order), so a multi-source read
                // is deterministic. Returns {items, dropped} where an item is
                //   {entry, src, srcLocal, also:[name], diverged, conflict,
                //    pwRefused}
                // and `src` is the provenance. It is stamped from the SOURCE LIST
                // onto a wrapper, never read off the value and never written onto
                // the entry: a row that lies about where it came from is exactly
                // the wrong thing to show, and any field on the entry is a field
                // whoever wrote that registry controls.
                //
                // De-dupe is by normalized URL and by URL ONLY. Two entries at one
                // address are one host — that is what a single-source classify has
                // always assumed. broker_id is NOT a merge key: it is unverified
                // input, so keying on it would let one source's record swallow
                // another's by claiming its identity, and a chain of records could
                // collapse two genuinely different brokers into one. Where two
                // ADDRESSES claim one broker_id, both rows survive and both are
                // flagged as a conflict — that is an identity disagreement to show
                // the user, not a duplicate to silently resolve.
                function mergeSources(sources, opts) {
                    opts = opts || {};
                    const byUrl = new Map();
                    const byBroker = new Map();
                    const items = [];
                    let droppedLoopback = 0;
                    for (const s of (Array.isArray(sources) ? sources : [])) {
                        const hosts = Array.isArray(s && s.hosts) ? s.hosts : [];
                        for (const raw of hosts) {
                            if (items.length >= REG_MAX_HOSTS) break;
                            const e = normEntry(raw);
                            if (!e) continue;
                            let pwRefused = false;
                            if (!s.local) {
                                // A loopback row names the PUBLISHER's machine.
                                if (isLoopbackOrigin(e.url)) {
                                    droppedLoopback += 1;
                                    continue;
                                }
                                // Never import an invisible host from somebody
                                // else's list: `hidden` takes it out of the host
                                // UI while the poll loop keeps talking to it.
                                e.hidden = false;
                                if (e.token && !opts.acceptRemoteTokens) {
                                    e.token = '';       // hasToken stays, so the
                                    pwRefused = true;   // row can say it was there
                                }
                            }
                            const prev = byUrl.get(e.url);
                            if (prev) {
                                if (prev.also.indexOf(s.name) === -1) {
                                    prev.also.push(s.name);
                                }
                                if (entryDiffers(prev.entry, e)) {
                                    prev.diverged = true;
                                }
                                continue;
                            }
                            const item = { entry: e, src: s.name,
                                           srcLocal: !!s.local, also: [],
                                           diverged: false, conflict: '',
                                           pwRefused: pwRefused };
                            byUrl.set(e.url, item);
                            if (e.brokerId) {
                                const other = byBroker.get(e.brokerId);
                                if (!other) {
                                    byBroker.set(e.brokerId, item);
                                } else if (other.entry.url !== e.url) {
                                    other.conflict = e.url;
                                    item.conflict = other.entry.url;
                                }
                            }
                            items.push(item);
                        }
                    }
                    return { items: items, droppedLoopback: droppedLoopback };
                }
                // Do two registry entries for the SAME url disagree about anything
                // a user would notice? Drives the "sources disagree" flag, so the
                // losing source is never silently discarded.
                function entryDiffers(a, b) {
                    return a.label !== b.label || a.url !== b.url
                        || a.color !== b.color || (!!a.hidden) !== (!!b.hidden)
                        || a.brokerId !== b.brokerId
                        || (a.token || '') !== (b.token || '')
                        || (!!a.hasToken) !== (!!b.hasToken);
                }
                // Classify every merged item vs the live local hosts, dropping
                // this broker (by origin OR broker_id). Takes mergeSources' items,
                // not a raw host array: the merge is what decides which array a
                // source contributes — a decrypted one, a plaintext projection, or
                // a plain v1 list — and it is the only place provenance exists.
                function classify(items) {
                    const myOrigin = localOrigin();
                    const myBrokerId = cleanStr(localHost().brokerId, 128);
                    const rows = [];
                    for (const it of (Array.isArray(items) ? items : [])) {
                        const e = it && it.entry;
                        if (!e) continue;
                        if (e.url === myOrigin) continue;               // us (url)
                        if (myBrokerId && e.brokerId === myBrokerId) continue; // us (id)
                        const local = matchLocal(e);
                        let row;
                        if (!local) {
                            row = { kind: 'new', entry: e, local: null,
                                    checked: true };
                        } else if (hostDiffers(local, e)) {
                            row = { kind: 'differs', entry: e, local: local,
                                    checked: false,
                                    urlChanged:
                                        normalizeHostUrl(local.url) !== e.url };
                        } else if (tokenDiffers(local, e)) {
                            row = { kind: 'differs', entry: e, local: local,
                                    checked: false, urlChanged: false,
                                    tokenOnly: true };
                        } else {
                            row = { kind: 'identical', entry: e, local: local,
                                    checked: false };
                        }
                        row.src = it.src;
                        row.srcLocal = it.srcLocal;
                        row.also = it.also;
                        row.diverged = it.diverged;
                        row.conflict = it.conflict;
                        row.pwRefused = it.pwRefused;
                        rows.push(row);
                    }
                    return rows;
                }
                // The single-source shorthand: merge one local source, classify.
                function classifyLocal(hosts) {
                    return classify(mergeSources(
                        [{ name: 'this broker', local: true, hosts: hosts }]).items);
                }
                // ---- pull sources (#174) ------------------------------------
                // Pull used to read only the broker whose page is open, which
                // made recovering a list published to broker B circular when B
                // was the very host missing from the list. It now reads any
                // configured broker, over the routing publish has always had
                // (ctx.serverStore.get({host})).
                //
                // Reading somebody else's broker means every failure has to be
                // distinguishable. The old code treated any non-200 as "no
                // registry yet" — right for the local broker, wrong for a remote
                // one, where a 401 means "it refused our password", not "empty".
                // Pure so it can be tested against the whole table.
                function sourceTransportState(got) {
                    const st = (got && typeof got.status === 'number')
                        ? got.status : -1;
                    const err = (got && typeof got.error === 'string')
                        ? got.error : '';
                    if (st === 0 || st === -1) {
                        if (err === 'no_host') {
                            return { state: 'no_host',
                                     why: 'no longer in your host list' };
                        }
                        if (err === 'timeout') {
                            return { state: 'unreachable',
                                     why: 'did not answer in time' };
                        }
                        return { state: 'unreachable',
                                 why: 'could not be reached' };
                    }
                    if (st === 401 || st === 403) {
                        return { state: 'unauthorized',
                                 why: 'refused our password — set it on the '
                                    + 'Browser tab' };
                    }
                    if (st === 404) {
                        return { state: 'unsupported',
                                 why: 'older build: it has no registry to read' };
                    }
                    if (st < 200 || st > 299) {
                        return { state: 'error', why: 'answered HTTP ' + st };
                    }
                    return { state: 'ok', why: '' };
                }
                // …and what a 2xx actually carries. 'locked' is only reported once
                // the envelope has passed structural validation — an envelope we
                // cannot parse is unreadable, and sending someone to a passphrase
                // prompt that cannot succeed is worse than saying so.
                function sourceContentState(value) {
                    const scope = encScope(value);
                    if (scope) {
                        const bad = (scope === 'unknown')
                            ? 'unknown scope' : encProblem(value.enc);
                        if (bad) {
                            return { state: 'unreadable', count: 0,
                                     why: 'its encrypted block is unreadable ('
                                        + bad + ')' };
                        }
                        const n = publicHostsOf(value).length;
                        return { state: 'locked', count: n,
                                 why: scope === 'all'
                                    ? 'encrypted — needs a passphrase'
                                    : n + ' host' + (n === 1 ? '' : 's')
                                      + ', passwords encrypted' };
                    }
                    const n = publicHostsOf(value).length;
                    if (!n) {
                        return { state: 'empty', count: 0,
                                 why: 'nothing published there' };
                    }
                    return { state: 'ok', count: n,
                             why: n + ' host' + (n === 1 ? '' : 's') };
                }
                function sourceState(got) {
                    const t = sourceTransportState(got);
                    if (t.state !== 'ok') return { state: t.state, why: t.why,
                                                   count: 0 };
                    return sourceContentState(got && got.value);
                }
                // Only these two have anything to read.
                function sourceUsable(state) {
                    return state === 'ok' || state === 'locked';
                }

                // Tamper evidence for a 'tokens' registry: the plaintext side is
                // a PROJECTION of what we sealed, and one buildValue writes both,
                // so they cannot drift on their own. A mismatch means somebody
                // edited the readable half. The decrypted half is used either way
                // (it is the authenticated one) — this only refuses to do it
                // silently.
                function encPublicMatches(value, hosts) {
                    const pub = publicHostsOf(value);
                    if (!Array.isArray(hosts) || pub.length !== hosts.length) {
                        return false;
                    }
                    for (let i = 0; i < pub.length; i++) {
                        const a = pub[i] || {}, b = hosts[i] || {};
                        if (cleanStr(a.url, URL_MAX) !== cleanStr(b.url, URL_MAX)
                                || cleanStr(a.id, 64) !== cleanStr(b.id, 64)
                                || cleanStr(a.label, LABEL_MAX)
                                    !== cleanStr(b.label, LABEL_MAX)) return false;
                    }
                    return true;
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
                                // #175: that holds for a LOCKED token too — if the
                                // registry's password is still in its envelope, the
                                // entry carries none, so the local one is cleared
                                // rather than pointed at a new address. The row's
                                // warning says so before you tick it.
                                local.url = row.entry.url;
                                local.token = row.entry.token || '';
                                // #174: CLEAR the identity rather than adopt the
                                // incoming one. Importing it wrote an unverified
                                // broker_id into prefs — contradicting the rule
                                // three lines up, and letting whoever wrote that
                                // registry choose what this host claims to be.
                                // refreshHostIdentities() re-learns it from the
                                // new address's own /info.
                                local.brokerId = '';
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
                //
                // `purge` rides every ENCRYPTED publish. The modstore keeps a
                // 50-deep revision ring, so the value this write replaces — the
                // plaintext one, tokens and all — would otherwise sit in the
                // history of the very store we just stopped trusting, and "the
                // broker only stores ciphertext" would be false the moment anyone
                // migrated. get() returns rev + timestamps only, never past
                // bodies, so this browser cannot tell whether the ring holds a
                // token; clearing it is the only honest answer. Nothing in this
                // mod reads the ring.
                async function publishTo(hid, value, purge) {
                    const host = (hid === 'local') ? localHost() : hostById(hid);
                    const name = (host && host.id === 'local')
                        ? 'this broker' : (host ? host.label : hid);
                    let got = null;
                    try { got = await ctx.serverStore.get({ host: hid }); } catch (_) {}
                    const rev = (got && typeof got.rev === 'number') ? got.rev : 0;
                    const opts = purge
                        ? { host: hid, purgeRevisions: true } : { host: hid };
                    let res = await ctx.serverStore.set(value, rev, opts);
                    if (res && res.status === 409 && res.error === 'conflict'
                            && typeof res.rev === 'number') {
                        res = await ctx.serverStore.set(value, res.rev, opts);
                    }
                    return { hid: hid, name: name, ok: !!(res && res.ok),
                             error: res && res.error };
                }
                async function doPublish(selected, includeTokens, localUrl, toAll) {
                    const value = buildValue(selected, includeTokens, localUrl);
                    if (!value.hosts.length) {
                        showNotice('Nothing to publish — no shareable hosts '
                            + 'selected (a loopback-only local address is skipped).');
                        return;
                    }
                    const mode = encMode();
                    const plan = encPlan(mode, value, encAvailable());
                    if (plan.blocked) {
                        showNotice('Publish cancelled. Broker registry encryption '
                            + 'is set to "' + encModeLabel(mode) + '", but '
                            + encWhyUnavailable() + ' Reach this broker over '
                            + 'https (or on localhost), or set encryption to Off '
                            + '— which publishes in the clear.',
                            { sticky: true, type: 'error' });
                        return;
                    }
                    let out = value;
                    if (plan.seal) {
                        const pass = await requirePassphrase();
                        if (!pass) return;          // cancelled -> publish NOTHING
                        showNotice('Encrypting the registry…');
                        try {
                            out = (plan.seal === 'all')
                                ? await sealAll(value, pass)
                                : await sealTokens(value, pass);
                        } catch (e) {
                            showNotice('Could not encrypt the registry ('
                                + ((e && e.message) || 'failed')
                                + '). Nothing was published.',
                                { sticky: true, type: 'error' });
                            return;
                        }
                    }
                    // Targets: the local broker always; every configured broker if
                    // "publish to all" was ticked. Each broker is an INDEPENDENT
                    // store, so results are reported per broker (never a blanket
                    // success when some failed). The value — ciphertext included —
                    // is built ONCE, so every broker gets the same bytes under one
                    // passphrase rather than a per-broker re-derive.
                    const targets = toAll ? getHosts().map(h => h.id) : ['local'];
                    const results = [];
                    for (const hid of targets) {
                        results.push(await publishTo(hid, out, !!plan.seal));
                    }
                    // Don't nudge yourself: the one-time discovery notice keys off
                    // the stored nonce, and the browser that just published one
                    // has nothing to be told about. Recorded only when the LOCAL
                    // write (the store that notice reads) actually landed.
                    if (results.some(r => r.hid === 'local' && r.ok)) {
                        try { ctx.storage.set(NOTIFIED_KEY, out.nonce); } catch (_) {}
                    }
                    const oks = results.filter(r => r.ok);
                    const fails = results.filter(r => !r.ok);
                    const how = plan.seal === 'all' ? ', encrypted (whole list)'
                        : plan.seal === 'tokens' ? ', passwords encrypted'
                        : (includeTokens ? ', including passwords' : '');
                    if (!fails.length) {
                        showNotice('Published to ' + oks.length + ' broker'
                            + (oks.length === 1 ? '' : 's') + how + '.',
                            (includeTokens && !plan.seal)
                                ? { sticky: true } : undefined);
                    } else {
                        const why = (f) => f.error === 'not_active'
                            ? 'another browser is active there'
                            : f.error === 'no_host' ? 'host not found'
                            : f.error === 'too_large'
                                ? 'the registry is too large for that broker'
                            : (f.error || 'failed');
                        showNotice('Published to ' + oks.length + ' of '
                            + results.length + ' brokers. Failed: '
                            + fails.map(f => f.name + ' (' + why(f) + ')').join('; '),
                            { sticky: true, type: 'error' });
                    }
                }

                // ---- forget passwords ---------------------------------------
                // The emergency path, and it must work with NO passphrase — so it
                // never decrypts anything. An envelope is opaque: this browser
                // cannot tell whether a password is inside one, so it assumes so.
                // A fresh object is built rather than the stored one edited, which
                // is what drops `enc`/`encNote` — the ciphertext goes with the
                // passwords it was hiding.
                function stripTokens(value) {
                    const scope = encScope(value);
                    let raw = [];
                    if (scope === 'tokens' || !scope) {
                        // 'tokens' keeps its readable list (publicHostsOf strips
                        // any token somebody added to it); a plain v1 keeps its
                        // own hosts, minus the tokens deleted below.
                        raw = publicHostsOf(value);
                    }
                    // scope 'all' / 'unknown' -> []. The whole list is inside the
                    // envelope, so passwords cannot be separated from it without
                    // the passphrase; the honest emergency answer is to drop both,
                    // and doForget says exactly that before you agree to it.
                    return { v: 1, updated: Math.floor(Date.now() / 1000),
                             nonce: makeNonce(),
                             origin: (value && cleanStr(value.origin, 128)) || '',
                             hosts: raw.map(function (h) {
                                 const c = Object.assign({}, h);
                                 delete c.token;
                                 delete c.hasToken;
                                 return c;
                             }) };
                }
                function valueHasTokens(value) {
                    if (encScope(value)) return true;   // opaque -> assume it does
                    const raw = (value && value.v === 1 && Array.isArray(value.hosts))
                        ? value.hosts : [];
                    return raw.some(h => h && typeof h.token === 'string' && h.token);
                }
                async function doForget() {
                    // Read BEFORE confirming, so the confirmation describes what
                    // will actually happen to THIS registry.
                    let got = null;
                    try { got = await ctx.serverStore.get(); } catch (_) {}
                    if (!got || got.status === 0) {
                        showNotice('Could not read the registry on this broker.',
                            { sticky: true, type: 'error' });
                        return;
                    }
                    const scope = encScope(got.value);
                    const whole = (scope === 'all' || scope === 'unknown');
                    const tail = 'This does not revoke access: a browser that '
                        + 'already pulled a password keeps it, and a broker\'s '
                        + 'token stays valid until you rotate it. Continue?';
                    const ok = await openConfirmDialog({
                        title: 'Forget passwords',
                        message: whole
                            ? 'This registry is encrypted as a WHOLE LIST, so '
                              + 'this browser cannot see which part of it is a '
                              + 'password without the passphrase. Continuing '
                              + 'removes the ENTIRE published list and its saved '
                              + 'history from this broker — not just the '
                              + 'passwords. ' + tail
                            : 'Remove every password from this broker\'s '
                              + 'registry AND its saved history. ' + tail,
                        okLabel: whole ? 'Remove the whole list'
                            : 'Forget passwords', danger: true });
                    if (!ok) return;
                    const rev = (typeof got.rev === 'number') ? got.rev : 0;
                    const stripped = stripTokens(got.value);
                    // purgeRevisions clears the ring so a token that scrolled into
                    // history is gone too. A fresh nonce makes the write non-deduped.
                    // NO conflict rebase here, unlike publish: the confirmation the
                    // user gave was about the value that was read above, and a
                    // registry that changed under them may have changed CLASS
                    // (a 'tokens' envelope replaced by a whole-list one), turning
                    // "remove the passwords" into "remove everything". Re-run it.
                    const res = await ctx.serverStore.set(stripped, rev,
                        { purgeRevisions: true });
                    if (!res || !res.ok) {
                        showNotice('Could not forget passwords: '
                            + (res && res.error === 'not_active'
                               ? 'another browser is active'
                               : res && res.error === 'conflict'
                               ? 'the registry changed while you were confirming '
                                 + '— try again'
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
                // A real type=password field, NOT one of openDialog's `fields`:
                // those are type=text (69_js_dialog.js), which would show the
                // passphrase and offer it to form autofill. Built in body() with
                // the inputs kept in a closure — the same shape the publish dialog
                // already uses for its url input.
                //   o = {mode:'set'|'unlock', whole:Boolean, error:String}
                //   -> null (cancel) | {skip:true} | {pass, confirm}
                async function openPassphraseDialog(o) {
                    let inp = null, inp2 = null;
                    const mkPass = function (labelText, auto) {
                        const row = document.createElement('div');
                        row.className = 'set-row';
                        const lab = document.createElement('label');
                        lab.textContent = labelText;
                        const i = document.createElement('input');
                        i.type = 'password';
                        i.autocomplete = auto;
                        i.spellcheck = false;
                        row.appendChild(lab);
                        row.appendChild(i);
                        return { row: row, input: i };
                    };
                    const buttons = [{ label: o.mode === 'set' ? 'Encrypt'
                        : 'Unlock', value: 'ok', primary: true }];
                    // Only a 'tokens' registry has anything readable to fall back
                    // to, so only it offers the skip.
                    if (o.mode === 'unlock' && !o.whole) {
                        buttons.push({ label: 'Without passwords', value: 'skip' });
                    }
                    // #174: with several sources selected, backing out has to skip
                    // THIS source rather than read as "give up on the pull" — and
                    // it must not quietly import the readable half instead, which
                    // is what the separate "Without passwords" button is for.
                    buttons.push({ label: o.source ? 'Skip this broker' : 'Cancel',
                                   value: false });
                    const who = o.source ? ('“' + o.source + '”') : 'This broker';
                    const pending = openDialog({
                        title: o.mode === 'set' ? 'Registry passphrase'
                            : (o.source ? ('Unlock ' + who) : 'Unlock the registry'),
                        body: function (c) {
                            const msg = document.createElement('div');
                            msg.className = 'app-dialog-msg';
                            msg.textContent = o.mode === 'set'
                                ? 'This passphrase encrypts the registry in this '
                                  + 'browser before it is published. It is never '
                                  + 'sent anywhere and never stored — lose it and '
                                  + 'you republish the list under a new one. '
                                  + 'Anyone who can read the broker\'s files can '
                                  + 'guess at it offline, so make it long.'
                                : (o.whole
                                    ? who + '’s registry is encrypted. '
                                      + 'Enter the passphrase it was published '
                                      + 'with.'
                                    : who + '’s passwords are encrypted. '
                                      + 'Enter the passphrase to import them, or '
                                      + 'import the list without passwords.');
                            c.appendChild(msg);
                            const a = mkPass('Passphrase', o.mode === 'set'
                                ? 'new-password' : 'current-password');
                            inp = a.input;
                            c.appendChild(a.row);
                            if (o.mode === 'set') {
                                const b = mkPass('Confirm', 'new-password');
                                inp2 = b.input;
                                c.appendChild(b.row);
                            }
                            const show = mkRowCheck(' Show the passphrase', false);
                            show.cb.addEventListener('change', function () {
                                const t = show.cb.checked ? 'text' : 'password';
                                if (inp) inp.type = t;
                                if (inp2) inp2.type = t;
                            });
                            c.appendChild(show.row);
                            if (o.error) {
                                const err = document.createElement('div');
                                err.className = 'set-hint hostreg-warn';
                                err.textContent = o.error;
                                c.appendChild(err);
                            }
                        },
                        buttons: buttons,
                    });
                    // openDialog mounts and focuses SYNCHRONOUSLY before it hands
                    // the promise back (the executor runs during construction), so
                    // this lands on the field rather than the primary button.
                    try { if (inp) inp.focus(); } catch (_) {}
                    const res = await pending;
                    const out = (!res || !res.value) ? null
                        : (res.value === 'skip') ? { skip: true }
                        : { pass: inp ? inp.value : '',
                            confirm: inp2 ? inp2.value : '' };
                    // Drop the DOM's copy; the overlay is already detached.
                    if (inp) inp.value = '';
                    if (inp2) inp2.value = '';
                    inp = inp2 = null;
                    return out;
                }
                // The session passphrase, prompting (with a confirm field) only
                // when none is held. Returns null if the user backs out, and the
                // caller MUST treat that as "publish nothing".
                async function requirePassphrase() {
                    if (_encPass) return _encPass;
                    let err = '';
                    for (;;) {
                        const r = await openPassphraseDialog({ mode: 'set',
                            error: err });
                        if (!r) return null;
                        const p = r.pass || '';
                        if (p.length < ENC_PASS_MIN) {
                            err = 'Use at least ' + ENC_PASS_MIN + ' characters.';
                            continue;
                        }
                        if (p !== r.confirm) {
                            err = 'The two passphrases don\'t match.';
                            continue;
                        }
                        _encPass = p;
                        refreshPane();
                        return p;
                    }
                }
                // Resolve ONE source's stored value to the host array to classify,
                // asking for a passphrase only when there is an envelope.
                //   -> {hosts, unlocked} | null (the user backed out of THIS
                //      source; with several selected that skips it, not the pull)
                // NOTHING local is touched here: a wrong passphrase costs a retry,
                // never a clobbered host list. #175's invariant holds per source —
                // the array returned is either the decrypted one, used alone, or
                // the plaintext projection, chosen explicitly. The two are never
                // spliced together.
                async function unlockForPull(value, srcName) {
                    const scope = encScope(value);
                    if (!scope) {
                        return { hosts: publicHostsOf(value), unlocked: false };
                    }
                    const whole = (scope !== 'tokens');
                    const who = srcName ? ('“' + srcName + '”') : 'this broker';
                    const fallback = function () {
                        return whole ? null
                            : { hosts: publicHostsOf(value), unlocked: false };
                    };
                    const problem = (scope === 'unknown')
                        ? 'unknown scope' : encProblem(value.enc);
                    if (problem) {
                        showNotice('The registry on ' + who + ' carries an '
                            + 'encrypted block this browser can\'t read ('
                            + problem + ').'
                            + (whole ? '' : ' Importing without passwords.'),
                            { sticky: true, type: whole ? 'error' : undefined });
                        return fallback();
                    }
                    if (!encAvailable()) {
                        showNotice((whole
                            ? 'The registry on ' + who + ' is encrypted and this '
                              + 'page can\'t decrypt it: '
                            : 'The passwords on ' + who + ' are encrypted and this '
                              + 'page can\'t decrypt them, so they are skipped: ')
                            + encWhyUnavailable(),
                            { sticky: true, type: whole ? 'error' : undefined });
                        return fallback();
                    }
                    let err = '';
                    // The session passphrase is TRIED, once, and never cleared by
                    // this source's failure (#174): with several sources selected,
                    // one tampered or differently-keyed registry would otherwise
                    // evict the passphrase that unlocks all the others. It is only
                    // PROMOTED to the session passphrase if none is held yet, so a
                    // second source's passphrase can never quietly become the one
                    // the next Publish encrypts with.
                    let held = _encPass;
                    for (;;) {
                        let pass = held;
                        held = null;                 // try the held one once only
                        if (!pass) {
                            const r = await openPassphraseDialog({ mode: 'unlock',
                                whole: whole, error: err, source: srcName });
                            if (!r) return null;             // skip this source
                            if (r.skip) {
                                return { hosts: publicHostsOf(value),
                                         unlocked: false };
                            }
                            pass = r.pass;
                        }
                        showNotice('Unlocking ' + who + '…');
                        let payload = null;
                        try {
                            payload = await encOpen(value.enc, pass);
                        } catch (e) {
                            if (e && e.encError === 'passphrase') {
                                // A wrong key and a modified blob are the SAME GCM
                                // failure; claiming "wrong passphrase" alone would
                                // send someone round a loop they cannot win.
                                err = 'Wrong passphrase — or the stored registry '
                                    + 'was modified after it was published.';
                                continue;
                            }
                            showNotice('Could not read the encrypted registry on '
                                + who + ' (' + ((e && e.message) || 'unreadable')
                                + ').', { sticky: true, type: 'error' });
                            return fallback();
                        }
                        if (!_encPass) { _encPass = pass; refreshPane(); }
                        if (!whole && !encPublicMatches(value, payload.hosts)) {
                            showNotice('The readable host list on ' + who + ' does '
                                + 'not match its encrypted contents — someone '
                                + 'edited the stored copy. Using the encrypted '
                                + '(authenticated) list.',
                                { sticky: true, type: 'error' });
                        }
                        return { hosts: payload.hosts, unlocked: true };
                    }
                }

                // ---- pull sources: probe + pick (#174) -----------------------
                // Every configured broker, in a FIXED order — this broker first,
                // then the host list's own order — so a multi-source read is
                // deterministic.
                function pullSourceHosts() {
                    const out = [localHost()];
                    for (const h of getHosts()) {
                        if (h && h.id !== 'local') out.push(h);
                    }
                    return out;
                }
                function srcLabel(h) {
                    if (!h || h.id === 'local') return 'this broker';
                    return cleanStr(h.label, LABEL_MAX) || h.url || h.id;
                }
                // hostFetch already deadlines the CONNECT, but fetch() resolves on
                // headers: a broker that answers 200 and then dribbles (or never
                // finishes) its body would hang the read forever, and with it the
                // source dialog. So each probe carries its own end-to-end deadline
                // covering the body, and one bad source can never hold up the rest.
                const PROBE_MS = 8000;
                function withDeadline(p, ms, onTimeout) {
                    return new Promise(function (resolve) {
                        let done = false;
                        const t = setTimeout(function () {
                            if (!done) { done = true; resolve(onTimeout); }
                        }, ms);
                        p.then(function (v) {
                            if (!done) { done = true; clearTimeout(t); resolve(v); }
                        }, function () {
                            if (!done) {
                                done = true; clearTimeout(t);
                                resolve({ status: 0, error: 'failed' });
                            }
                        });
                    });
                }
                async function probeSource(host) {
                    const local = (host.id === 'local');
                    const rec = { hostId: host.id, name: srcLabel(host),
                                  local: local, value: null };
                    // A host with no saved password can only 401. Don't make the
                    // request: it tells that broker we tried and learns nothing we
                    // don't already know from our own host list.
                    if (!local && !(host.token)) {
                        return Object.assign(rec, { state: 'no_password', count: 0,
                            why: 'no password saved — set it on the Browser tab' });
                    }
                    const got = await withDeadline(
                        ctx.serverStore.get({ host: host.id }), PROBE_MS,
                        { status: 0, error: 'timeout' });
                    rec.value = got && got.value;
                    return Object.assign(rec, sourceState(got));
                }
                // The source picker. Returns {sources:[rec], acceptRemoteTokens}
                // or null. Skipped entirely when nothing but this broker is
                // configured, so the single-broker case is exactly as it was.
                async function pickSources() {
                    const hosts = pullSourceHosts();
                    if (hosts.length < 2) {
                        const only = await probeSource(hosts[0]);
                        return { sources: [only], acceptRemoteTokens: false,
                                 skipped: [], single: true };
                    }
                    showNotice('Checking ' + hosts.length + ' brokers…');
                    // Isolated per source: probeSource never rejects, so one bad
                    // broker cannot take the whole dialog down with it.
                    const recs = await Promise.all(hosts.map(probeSource));
                    const refs = [];
                    let allBox = null, pwBox = null;
                    const anyRemoteUsable = recs.some(
                        r => !r.local && sourceUsable(r.state));
                    const res = await openDialog({
                        title: 'Pull broker list',
                        body: function (c) {
                            const intro = document.createElement('div');
                            intro.className = 'app-dialog-msg';
                            intro.textContent = 'Which brokers\' lists to read. '
                                + 'Each one stores its own; reading more than one '
                                + 'merges them, and where two lists hold the same '
                                + 'address the first broker in this order wins.';
                            c.appendChild(intro);
                            const list = document.createElement('div');
                            list.className = 'hostreg-list';
                            for (const rec of recs) {
                                const usable = sourceUsable(rec.state);
                                const r = mkRowCheck('', usable && rec.local);
                                r.row.classList.add('hostreg-src');
                                if (!usable) {
                                    r.cb.disabled = true;
                                    r.row.classList.add('hostreg-identical');
                                }
                                const tag = document.createElement('span');
                                tag.className = 'hostreg-tag';
                                tag.textContent = rec.state;
                                r.span.textContent = rec.name;
                                r.row.insertBefore(tag, r.span);
                                if (rec.why) {
                                    const w = document.createElement('span');
                                    w.className = usable
                                        ? 'hostreg-tag' : 'hostreg-locked';
                                    w.textContent = ' — ' + rec.why;
                                    r.span.appendChild(w);
                                }
                                list.appendChild(r.row);
                                refs.push({ rec: rec, cb: r.cb, usable: usable });
                            }
                            c.appendChild(list);
                            const ar = mkRowCheck('Every broker that can be read',
                                false);
                            allBox = ar.cb;
                            allBox.addEventListener('change', function () {
                                for (const ref of refs) {
                                    if (ref.usable) ref.cb.checked = allBox.checked;
                                }
                            });
                            c.appendChild(ar.row);
                            if (anyRemoteUsable) {
                                const pr = mkRowCheck(
                                    'Accept passwords from other brokers', false);
                                pr.row.classList.add('hostreg-danger');
                                pwBox = pr.cb;
                                c.appendChild(pr.row);
                                const ph = document.createElement('div');
                                ph.className = 'set-hint hostreg-warn';
                                ph.textContent = 'Off by default. A password in '
                                    + 'another broker\'s list was put there by '
                                    + 'whoever wrote it, and taking it means this '
                                    + 'browser will send it to the address that '
                                    + 'list names — the same lateral-movement risk '
                                    + 'as publishing one, in the other direction. '
                                    + 'You still tick each host by hand afterwards.';
                                c.appendChild(ph);
                            }
                        },
                        buttons: [
                            { label: 'Read', value: 'read', primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    });
                    if (!res || res.value !== 'read') return null;
                    const chosen = refs.filter(r => r.usable && r.cb.checked)
                        .map(r => r.rec);
                    return { sources: chosen,
                             acceptRemoteTokens: !!(pwBox && pwBox.checked),
                             skipped: [], single: false };
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
                            // #175: what encryption is about to do, stated here so
                            // a passphrase request, or a refusal, is never a
                            // surprise arriving after the Publish click.
                            const eh = document.createElement('div');
                            eh.className = 'set-hint hostreg-status';
                            eh.textContent = encPublishHint();
                            c.appendChild(eh);
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
                    const picked = await pickSources();
                    if (!picked) return;                     // cancelled the picker
                    if (!picked.sources.length) {
                        showNotice('No broker selected to read.');
                        return;
                    }
                    // Single-source (nothing but this broker configured) keeps the
                    // old reporting exactly: a transport failure is flagged, and
                    // anything else is "no registry yet".
                    if (picked.single) {
                        const only = picked.sources[0];
                        if (only.state === 'unreachable'
                                || only.state === 'no_host') {
                            showNotice('Could not read the registry on this broker.',
                                { sticky: true, type: 'error' });
                            return;
                        }
                    }
                    // Unlock each selected source in turn. A source that will not
                    // open is ONE SKIPPED SOURCE, not a dead dialog.
                    const feeds = [];
                    const skipped = [];
                    for (const rec of picked.sources) {
                        const opened = await unlockForPull(rec.value,
                            picked.single ? '' : rec.name);
                        if (!opened) { skipped.push(rec.name); continue; }
                        feeds.push({ name: rec.name, local: rec.local,
                                     hosts: opened.hosts });
                    }
                    if (!feeds.length) {
                        showNotice(skipped.length
                            ? 'Nothing read — every selected broker was skipped.'
                            : 'No hosts to import.');
                        return;
                    }
                    const merged = mergeSources(feeds,
                        { acceptRemoteTokens: picked.acceptRemoteTokens });
                    const rows = classify(merged.items);
                    const multi = feeds.length > 1
                        || feeds.some(f => !f.local);
                    if (!rows.length) {
                        showNotice('No hosts to import from '
                            + (multi ? 'the selected brokers.'
                                     : 'this broker\'s registry.'));
                        return;
                    }
                    const refs = [];
                    const res = await openDialog({
                        title: 'Pull broker list',
                        body: function (c) {
                            const intro = document.createElement('div');
                            intro.className = 'app-dialog-msg';
                            intro.textContent = 'Import hosts published to '
                                + (multi
                                    ? ('these brokers: '
                                       + feeds.map(f => f.name).join(', ') + '.')
                                    : 'this broker.')
                                + ' New hosts are checked; hosts you already '
                                + 'have that differ are unchecked (your local copy '
                                + 'wins unless you tick them).';
                            c.appendChild(intro);
                            const notes = [];
                            if (skipped.length) {
                                notes.push('Skipped: ' + skipped.join(', ') + '.');
                            }
                            if (merged.droppedLoopback) {
                                notes.push(merged.droppedLoopback + ' loopback '
                                    + 'address' + (merged.droppedLoopback === 1
                                        ? ' was' : 'es were')
                                    + ' dropped — they only mean something on the '
                                    + 'machine that published them.');
                            }
                            if (notes.length) {
                                const n = document.createElement('div');
                                n.className = 'set-hint';
                                n.textContent = notes.join(' ');
                                c.appendChild(n);
                            }
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
                                    pw.textContent = row.srcLocal
                                        ? ' (has password)'
                                        : ' (password from ' + row.src + ')';
                                    r.span.appendChild(pw);
                                } else if (row.entry.hasToken) {
                                    // Either still in its envelope, or a remote
                                    // password we were told not to accept.
                                    const pw = document.createElement('span');
                                    pw.className = 'hostreg-locked';
                                    pw.textContent = row.pwRefused
                                        ? ' (password not accepted)'
                                        : ' (password locked)';
                                    r.span.appendChild(pw);
                                }
                                // #174: where this row came from. Shown whenever
                                // ANY selected source was remote, not only when
                                // several were — "which machine told me this" is
                                // the thing a user needs to weigh a row.
                                if (multi) {
                                    const s = document.createElement('span');
                                    s.className = 'hostreg-tag';
                                    s.textContent = ' from ' + row.src
                                        + ((row.also && row.also.length)
                                            ? (', also in ' + row.also.join(', '))
                                            : '');
                                    r.span.appendChild(s);
                                }
                                if (row.kind === 'differs' && row.urlChanged) {
                                    const w = document.createElement('div');
                                    w.className = 'set-hint hostreg-warn';
                                    w.textContent = 'URL differs — applying replaces '
                                        + 'your local one and clears its saved '
                                        + 'password (you may need to re-enter it).'
                                        + ((!row.entry.token && row.entry.hasToken)
                                            ? ' The registry\'s password for it is '
                                              + 'still locked, so it can\'t be '
                                              + 'imported in its place.' : '');
                                    r.row.appendChild(w);
                                }
                                if (row.tokenOnly) {
                                    const w = document.createElement('div');
                                    w.className = 'set-hint hostreg-warn';
                                    w.textContent = 'Same host, different password '
                                        + '— applying replaces the one you have '
                                        + 'saved for it.';
                                    r.row.appendChild(w);
                                }
                                if (row.diverged) {
                                    const w = document.createElement('div');
                                    w.className = 'set-hint hostreg-warn';
                                    w.textContent = 'The brokers that list this '
                                        + 'address don\'t agree about it — '
                                        + row.src + '\'s copy is the one shown.';
                                    r.row.appendChild(w);
                                }
                                if (row.conflict) {
                                    const w = document.createElement('div');
                                    w.className = 'set-hint hostreg-warn';
                                    w.textContent = 'Identity conflict — this '
                                        + 'address and ' + row.conflict + ' both '
                                        + 'claim to be the same broker. At most '
                                        + 'one of them is.';
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
                        // Guard against a double-open while a dialog is already up,
                        // AND against starting a second registry action while one
                        // is between dialogs (mid-derive, mid-publish) — openDialog
                        // is a singleton and would cancel the first one's prompt.
                        if (typeof isAppDialogOpen === 'function'
                                && isAppDialogOpen()) return;
                        if (_encBusy) return;
                        _encBusy = true;
                        Promise.resolve()
                            .then(onClick)
                            .catch(function (err) {
                                console.error('[host-registry]', err);
                            })
                            .then(function () { _encBusy = false; });
                    });
                    return b;
                }
                let _paneStatus = null, _paneForget = null;
                // One line describing what encryption will do on the NEXT publish.
                // Built locally from the setting and this page's capability —
                // never from anything the stored registry says.
                function encStatusText() {
                    const mode = encMode();
                    let s;
                    if (!encAvailable()) {
                        s = 'Encryption: unavailable here — ' + encWhyUnavailable()
                            + (mode === 'off' ? ''
                                : ' Publishing is blocked while the mode is "'
                                  + encModeLabel(mode) + '"; set it to Off to '
                                  + 'publish in the clear.');
                    } else if (mode === 'all') {
                        s = 'Encryption: whole list. Publishing asks for a '
                            + 'passphrase and the broker stores only ciphertext — '
                            + 'not even the labels.';
                    } else if (mode === 'tokens') {
                        s = 'Encryption: passwords only. A passphrase is asked for '
                            + 'when a publish actually carries passwords; labels '
                            + 'and addresses stay readable.';
                    } else {
                        s = 'Encryption: off — the list is published in the clear.';
                    }
                    if (_encPass) s += ' A passphrase is held for this session.';
                    return s;
                }
                function encPublishHint() {
                    const mode = encMode();
                    if (!encAvailable()) {
                        return mode === 'off'
                            ? 'Encryption is off — this list is published in the '
                              + 'clear.'
                            : 'Encryption is set to "' + encModeLabel(mode)
                              + '" but ' + encWhyUnavailable()
                              + ' Publishing will be refused.';
                    }
                    if (mode === 'all') {
                        return 'Encryption: whole list. '
                            + (_encPass ? 'The passphrase held for this session is '
                                + 'used — Forget passphrase (below the buttons) to '
                                + 'switch.' : 'You will be asked for a passphrase.');
                    }
                    if (mode === 'tokens') {
                        return 'Encryption: passwords only — engaged only if you '
                            + 'tick Include passwords. '
                            + (_encPass ? 'The passphrase held for this session is '
                                + 'used — Forget passphrase (below the buttons) to '
                                + 'switch.' : 'You will be asked for a passphrase.');
                    }
                    return 'Encryption is off — this list, and any password in it, '
                        + 'is published in the clear.';
                }
                function refreshPane() {
                    if (_paneStatus) _paneStatus.textContent = encStatusText();
                    if (_paneForget) _paneForget.disabled = !_encPass;
                }
                function forgetPassphrase() {
                    _encPass = null;
                    refreshPane();
                    showNotice('Passphrase dropped from this browser\'s memory.');
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
                    _paneForget = mkBtn('Forget passphrase',
                        'drop the encryption passphrase held for this session',
                        forgetPassphrase);
                    row.appendChild(_paneForget);
                    wrap.appendChild(row);
                    const status = document.createElement('div');
                    status.className = 'set-hint hostreg-status';
                    _paneStatus = status;
                    wrap.appendChild(status);
                    refreshPane();
                    return wrap;
                }
                ctx.registerSettingsPane({
                    id: 'host-registry',
                    mount: 'browser',
                    title: 'Broker registry',
                    render: renderPane,
                    // The buttons themselves need no reflect — the dialogs read
                    // live getHosts()/the registry each time they open. The
                    // encryption status line does: the mode is a SYNCED setting,
                    // so another browser can change it under this one.
                    reflect: refreshPane,
                });

                // #175: the mode. A synced setting, so every browser you own
                // publishes the same way. Availability is deliberately NOT folded
                // into it: all three options are always registered, so a page that
                // cannot encrypt still READS (and never clobbers) an intent it
                // can't act on — which is what lets encPlan refuse instead of
                // silently downgrading to a plaintext publish. Registered AFTER
                // the pane so the actions sit above the setting that qualifies
                // them; _encSetting is a pre-seeded `let` so the pane's
                // registration-time reflect() can't read it in its dead zone.
                const encOptions = encAvailable() ? [
                    { value: 'tokens', label: 'Passwords only (recommended)' },
                    { value: 'all', label: 'Whole list' },
                    { value: 'off', label: 'Off — publish in the clear' },
                ] : [
                    { value: 'tokens',
                      label: 'Passwords only — UNAVAILABLE on this page' },
                    { value: 'all', label: 'Whole list — UNAVAILABLE on this page' },
                    { value: 'off', label: 'Off — publish in the clear' },
                ];
                _encSetting = ctx.settings.select('hostRegEncrypt', encOptions, {
                    mount: 'browser',
                    title: 'Broker registry encryption',
                    label: 'Encrypt before publishing',
                    // Default 'passwords only'. It costs an existing user nothing
                    // — with Include passwords off (its own default) there is no
                    // secret in the value, so the publish is byte-identical to
                    // today's and no passphrase is asked for. It engages exactly
                    // when a publish would otherwise write a token to disk in the
                    // clear.
                    def: 'tokens',
                });
                _encSetting.onChange(function () { refreshPane(); });
                refreshPane();                  // now that the mode is readable
                ctx.onUnload(function () { _encPass = null; });

                // ---- one-time discovery notice ------------------------------
                // On init, one GET of the LOCAL registry: if it holds hosts this
                // browser lacks, nudge the user toward Control Panel -> Browser.
                // Keyed by the registry NONCE (changes every publish, survives a
                // broker restart / rev reset), recorded at SHOW time (showNotice
                // has no dismiss callback), so it fires once per distinct publish.
                // It NEVER asks for a passphrase: this runs with no user
                // interaction at all, and a page-load password prompt is exactly
                // the habit an attacker wants people to have.
                //
                // #174 deliberately left it LOCAL-ONLY while Pull went
                // multi-broker. Fanning it out would spend one request per
                // configured broker on every single page load, and give every one
                // of them a per-load failure to report, to buy a nudge toward an
                // action that now reaches all of them anyway.
                (async function () {
                    let got = null;
                    try { got = await ctx.serverStore.get(); } catch (_) {}
                    const value = got && got.value;
                    if (!value || typeof value !== 'object') return;
                    const nonce = (typeof value.nonce === 'string') ? value.nonce : '';
                    if (!nonce || ctx.storage.get(NOTIFIED_KEY) === nonce) return;
                    const scope = encScope(value);
                    if (scope === 'all' || scope === 'unknown') {
                        // Opaque without the passphrase — say what it is and stop.
                        ctx.storage.set(NOTIFIED_KEY, nonce);
                        showNotice(encAvailable()
                            ? 'This broker has an ENCRYPTED shared host list — '
                              + 'Control Panel → Browser → Broker registry → Pull '
                              + 'to unlock it.'
                            : 'This broker has an ENCRYPTED shared host list, and '
                              + 'this page can\'t decrypt it: '
                              + encWhyUnavailable(),
                            { sticky: true });
                        return;
                    }
                    if (value.v !== 1 || !Array.isArray(value.hosts)) return;
                    ctx.storage.set(NOTIFIED_KEY, nonce);   // record at show time
                    const newCount = classifyLocal(publicHostsOf(value))
                        .filter(r => r.kind === 'new').length;
                    if (!newCount) return;
                    showNotice('This broker has a shared host list with ' + newCount
                        + ' host' + (newCount === 1 ? '' : 's') + ' you don\'t have '
                        + 'yet — Control Panel → Browser → Broker registry → Pull.',
                        { sticky: true });
                })();
            },
        });
