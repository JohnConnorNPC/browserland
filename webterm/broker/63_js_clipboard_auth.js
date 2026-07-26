        // ---- clipboard ----------------------------------------------------
        // The broker is commonly served on http://<LAN-IP>:4445, which
        // browsers treat as a non-secure context. navigator.clipboard.*
        // is gated on isSecureContext, so on that origin both copy and
        // paste fall through to the legacy paths below.
        function canReadClipboard() {
            return !!(window.isSecureContext
                && navigator.clipboard
                && navigator.clipboard.readText);
        }
        function canWriteClipboardModern() {
            return !!(window.isSecureContext
                && navigator.clipboard
                && navigator.clipboard.writeText);
        }
        // ---- clipboard images (#137) ----------------------------------------
        // clipboard.read() (unlike readText) can return image/* items — the
        // capture side of image paste. Secure contexts only; on plain http the
        // Ctrl+V DOM-paste path still carries images via the gesture-scoped
        // clipboardData, which needs no API permission.
        function canReadClipboardItems() {
            return !!(window.isSecureContext
                && navigator.clipboard
                && navigator.clipboard.read);
        }
        async function readClipboardImageBlob() {
            // First image/* blob on the clipboard, or null. TEXT WINS: any
            // text/plain item -> null, so copying from apps that put both text
            // and a rendered bitmap on the clipboard (Word, browsers) pastes
            // the text, as a terminal user expects. Throws propagate to the
            // caller (permission denied / unsupported browser).
            const items = await navigator.clipboard.read();
            for (const item of items) {
                if (item.types.indexOf('text/plain') !== -1) return null;
            }
            for (const item of items) {
                for (const type of item.types) {
                    if (type.indexOf('image/') === 0) {
                        return await item.getType(type);
                    }
                }
            }
            return null;
        }
        function blobToBase64(blob) {
            // FileReader dataURL with the data:...;base64, prefix stripped —
            // same idiom as the file manager's readB64.
            return new Promise((resolve, reject) => {
                const fr = new FileReader();
                fr.onload = () => {
                    const s = String(fr.result || '');
                    const i = s.indexOf(',');
                    resolve(i === -1 ? '' : s.slice(i + 1));
                };
                fr.onerror = () => reject(fr.error || new Error('read failed'));
                fr.readAsDataURL(blob);
            });
        }
        function quotePathForPrompt(p) {
            // Double-quote only when whitespace forces it: Claude Code (and
            // shells) take a bare path fine, and quoting matches what its own
            // drag-and-drop inserts on Windows.
            return /\s/.test(p) ? '"' + p + '"' : p;
        }
        // ---- clipboard observers (#106) -----------------------------------
        // An explicit, reviewable notify registry so a mod (the #106 clipboard
        // history mod) can watch every copy-OUT / paste-IN WITHOUT monkey-patching
        // core. Additive: core calls _notifyClipboard at its copy/paste seams
        // (copyTextToClipboard below + the terminal paste listeners in
        // 67_js_window_lifecycle.js); the Set is empty — a clean no-op — until a
        // mod registers via addClipboardObserver (exposed as ctx.clipboard.observe).
        // Each observer is isolated so one bad handler can't break a copy/paste or a
        // sibling. Nothing is captured while no mod is enabled, so the default
        // posture records nothing (the history mod ships default-off — clipboards
        // carry secrets).
        const _clipboardObservers = new Set();
        function _notifyClipboard(dir, text) {
            if (!text) return;
            for (const fn of Array.from(_clipboardObservers)) {
                try { fn(dir, text); } catch (_) {}
            }
        }
        function addClipboardObserver(fn) {
            _clipboardObservers.add(fn);
            return function () { _clipboardObservers.delete(fn); };
        }
        // Returns Promise<boolean> — did the text actually reach the clipboard?
        // (#153) It used to return undefined and swallow every failure, which
        // was fine while every caller was a user gesture that could see the
        // result for itself. An unprompted PTY-driven write has to TELL the user
        // what happened, and it cannot tell the truth about a promise it never
        // looked at. Existing callers ignore the value and are unaffected.
        function copyTextToClipboard(text) {
            if (!text) return Promise.resolve(false);
            _notifyClipboard('out', text);   // #106: one call covers both write paths
            if (canWriteClipboardModern()) {
                return navigator.clipboard.writeText(text)
                    .then(() => true)
                    .catch(() => copyTextLegacy(text));
            }
            return Promise.resolve(copyTextLegacy(text));
        }
        function copyTextLegacy(text) {
            // execCommand('copy') still works in non-secure contexts as
            // long as it runs inside a user gesture (mouseup, keydown).
            // Stash a hidden textarea, select it, copy, restore focus to
            // whatever xterm.js had so further keystrokes still hit the
            // terminal.
            const prev = document.activeElement;
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            ta.style.top = '0';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            let ok = false;
            try {
                ta.select();
                ta.setSelectionRange(0, text.length);
                ok = !!document.execCommand('copy');
            } catch (e) {
                console.debug('legacy copy failed:', e);
            } finally {
                document.body.removeChild(ta);
                try { prev && prev.focus && prev.focus(); } catch (_) {}
            }
            return ok;
        }

        // ---- OSC 52: PTY-driven clipboard write (#153) ----------------------
        // A TUI that cannot reach the system clipboard itself asks the terminal
        // to do it: `ESC ] 52 ; Pc ; <base64> BEL`. xterm.js 5.3.0 registers no
        // handler for OSC 52, so until now the sequence was parsed and silently
        // discarded — "copy" inside lazygit/fzf/an agent CLI did nothing, with
        // no error and no clue.
        //
        // Everything downstream of the PTY is untrusted (a `cat` of a hostile
        // file, a dependency's build output, an SSH session to someone else's
        // box), and Browserland's amplification over a local terminal is that
        // the write crosses the network to the user's own laptop. So this is
        // default-OFF, opt-in per host, and gated.
        //
        // ARBITRATION IS BROWSER-GLOBAL even though parsing is per terminal:
        // the clipboard and the user-activation state belong to the DOCUMENT,
        // not to a terminal. Per-terminal limiters would let two terminals
        // evade the limit in parallel and let their writeText promises resolve
        // out of order, so a notice could name the wrong source. Every request
        // therefore carries an immutable identity captured at request time and
        // routes through the single policy below.
        //
        // NOT implemented, deliberately: the `?` query form, which asks the
        // terminal to REPLY with the clipboard on the PTY. That is clipboard
        // exfiltration to whatever is running in the terminal. It is refused in
        // code, not behind a setting anyone can flip.

        // Authorization lives under a DEDICATED local key, keyed per host —
        // never prefs/_settings, which sync to the broker's /state. That is the
        // point: a compromised broker must not be able to grant itself
        // clipboard write by editing its own settings blob. Per host, because
        // trusting your laptop's broker should not silently extend to every
        // remote box you attach to. Absent/junk entry = OFF.
        const OSC52_HOSTS_KEY = 'webterm:osc52Hosts:v1';
        function loadOsc52Hosts() {
            try {
                const o = JSON.parse(localStorage.getItem(OSC52_HOSTS_KEY) || '{}');
                return (o && typeof o === 'object' && !Array.isArray(o)) ? o : {};
            } catch (e) { return {}; }
        }
        // The entry key binds the host id to the URL it pointed at when the user
        // authorized it, so re-pointing an id REVOKES rather than inherits. Host
        // ids are not as immutable as they look: the #65 broker-registry mod
        // publishes prefs._hosts — ids included — between brokers, so an id you
        // once trusted can come back attached to a different machine. What the
        // user agreed to was that machine, not that string. A host whose record
        // is gone returns null and therefore reads as OFF, so removal and any
        // lookup with a junk id both fail SHUT.
        // No "|| 'local'" default here, deliberately. openWindow always resolves
        // a real hostId, so a missing one means something is wrong — and the one
        // thing a security gate must not do when it is confused is fall back to
        // the host the user is most likely to have authorized.
        //
        // JSON, not `id + '|' + url`: a separator that can appear in either half
        // lets two different host records spell the same key.
        function _osc52HostKey(hostId) {
            const h = hostById(hostId);
            if (!h) return null;
            return JSON.stringify([h.id, h.url || '']);
        }
        // Every key that no longer names a CURRENT host is dropped, and the
        // pruned map is written back. Without this, re-pointing a host's URL
        // only SUSPENDS the grant — point it back and the stale entry re-arms
        // it with no fresh consent, while the Control Panel box has been
        // reporting "off" the whole time. Removing a host drops its grant too,
        // so re-adding one always starts shut.
        function _osc52PruneStale(map) {
            let live;
            try { live = new Set(getHosts().map(h => JSON.stringify([h.id, h.url || '']))); }
            catch (e) { return map; }        // never fail a gate on a bad host list
            let changed = false;
            for (const k of Object.keys(map)) {
                if (!live.has(k)) { delete map[k]; changed = true; }
            }
            if (changed) {
                try { localStorage.setItem(OSC52_HOSTS_KEY, JSON.stringify(map)); }
                catch (e) {}
            }
            return map;
        }
        function isOsc52Allowed(hostId) {
            const k = _osc52HostKey(hostId);
            return k !== null && _osc52PruneStale(loadOsc52Hosts())[k] === true;
        }
        function setOsc52Allowed(hostId, on) {
            const k = _osc52HostKey(hostId);
            if (k === null) return;
            const map = _osc52PruneStale(loadOsc52Hosts());
            if (on) map[k] = true; else delete map[k];
            try { localStorage.setItem(OSC52_HOSTS_KEY, JSON.stringify(map)); }
            catch (e) { /* quota/private mode: the gate stays shut, which is safe */ }
        }

        // 1 MiB decoded. Checked as a base64-LENGTH bound before atob(), so a
        // payload we are about to reject is never allocated. Rejected, never
        // truncated: a half-copied `rm -rf /home/me/project` is a foot-gun of
        // exactly the wrong shape.
        const OSC52_MAX_BYTES = 1024 * 1024;
        const OSC52_MAX_B64 = Math.ceil(OSC52_MAX_BYTES / 3) * 4;
        // Activation window: a real TUI copy follows a keypress or a mouse
        // gesture within a second or so. Keyed on user INPUT, not on keydown —
        // a key-only check rejects touch and mouse-driven copies and misfires
        // around IME composition.
        const OSC52_ACTIVITY_MS = 5000;
        // Rate limit, stated explicitly rather than left to the reader:
        // BROWSER-GLOBAL (see the arbitration note above), 5 writes per rolling
        // 10 s. Only writes that pass every other gate consume quota — a
        // refusal already cost the caller a notice and never touched the
        // clipboard, and charging refusals would let a hostile program lock the
        // user out of their own legitimate copy.
        const OSC52_RATE_MAX = 5;
        const OSC52_RATE_WINDOW_MS = 10000;
        // Notices are coalesced per reason (not suppressed): a program in a
        // loop must not be able to bury the desktop in toasts, but every
        // DISTINCT reason still surfaces. A silent rejection would recreate the
        // exact symptom this whole feature exists to fix.
        const OSC52_NOTICE_THROTTLE_MS = 3000;

        // How long before a host that is switched OFF may say so again. The
        // one-shot must not be a ONE-shot-per-load: a program that emits an OSC
        // 52 at startup would otherwise consume it, and the user's own copy ten
        // minutes later would fail in exactly the silence this feature exists
        // to remove.
        const OSC52_REOFFER_MS = 60000;

        const _osc52 = {
            accepted: [],          // ms timestamps of accepted writes (rolling)
            noticedAt: new Map(),  // reason+window -> last notice ms
            offeredAt: new Map(),  // host key -> last "it's off" notice ms
        };
        // Coalesced per reason AND per window. Keying on the reason alone would
        // let a hostile terminal swallow another terminal's refusal by firing
        // the same reason first — the victim's copy would then do nothing, with
        // nothing said, which is the original bug wearing a new hat.
        function _osc52Notice(reason, req, text, opts) {
            const now = Date.now();
            const k = reason + '\n' + (req ? req.winId : '');
            const last = _osc52.noticedAt.get(k) || 0;
            if (now - last < OSC52_NOTICE_THROTTLE_MS) return;
            _osc52.noticedAt.set(k, now);
            showNotice(text, opts);
        }
        // Attribution comes from application-owned facts ONLY — the broker's
        // session id and the user's own host label. NEVER the window title:
        // titles are settable straight from the PTY via OSC 0/2, so a hostile
        // program could otherwise forge the attribution on its own clipboard
        // write.
        //
        // An unknown host reads as "an unknown host", NOT as "this broker".
        // Naming the local broker for a request that did not come from it would
        // send a user following the advice to authorize the one host most worth
        // protecting — the same fallback _osc52HostKey deliberately refuses.
        function _osc52Where(req) {
            const h = hostById(req.hostId);
            return !h ? 'an unknown host'
                : (h.id === 'local' ? 'this broker' : h.label);
        }
        function _osc52Who(req) {
            return 'terminal #' + req.sid + ' on ' + _osc52Where(req);
        }

        // Decode Pd. Returns {text} | {error}. Never throws.
        function _osc52Decode(pd) {
            // Tolerate the shapes real emitters produce: base64(1) wraps at 76
            // columns, some tools omit padding, some use the URL-safe alphabet.
            //
            // ASCII whitespace only, NOT \s. \s also matches U+00A0 and friends,
            // which would let a payload carrying a non-breaking space decode
            // here while the agent-side snapshot strip — which works on bytes
            // and stops at \xc2 — leaves that same sequence in the ring replay.
            // Keeping the two in step costs one character class.
            // Length-check the RAW string first. The three rewrites below each
            // allocate a full copy, and xterm buffers up to PAYLOAD_LIMIT (1e7)
            // before handing it over — so checking after them would mean ~3
            // copies of a payload we are about to reject. Whitespace can only
            // shrink the string, so a raw length over the bound is over the
            // bound however it is spelled.
            if (String(pd).length > OSC52_MAX_B64) return { error: 'too-large' };
            const b64 = String(pd).replace(/[ \t\r\n\f\v]+/g, '')
                .replace(/-/g, '+').replace(/_/g, '/');
            if (!b64) return { error: 'empty' };
            if (b64.length > OSC52_MAX_B64) return { error: 'too-large' };
            let bin;
            try {
                bin = atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
            } catch (e) { return { error: 'bad-base64' }; }
            if (bin.length > OSC52_MAX_BYTES) return { error: 'too-large' };
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            let text;
            try {
                // fatal:true ON PURPOSE. OSC 52 carries BYTES, not a declared
                // charset, and non-UTF-8 payloads really happen (a remote box
                // in a legacy locale, a byte-oriented tool). The default
                // non-fatal mode substitutes U+FFFD and reports SUCCESS — it
                // would claim a copy while silently mutating it.
                text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
            } catch (e) { return { error: 'not-utf8' }; }
            // Reject on a control character; do NOT strip. A silent C0 strip is
            // the worst option available: it corrupts legitimate copies that
            // contain ESC/FF/NUL/BS, can CONCATENATE text that was separated,
            // and does not even buy safety — the characters that make
            // pastejacking dangerous (CR, LF, TAB) are exactly the ones a strip
            // would keep. Exact copy semantics, or a visible refusal.
            // The range runs to \x9f, not \x7f: C1 is the 8-bit spelling of the
            // same controls, and the vendored parser honours it — U+009B is
            // CSI and U+009D is OSC. Stopping at DEL would reject `a\x1bb` and
            // wave through its exact twin spelled with the single C1 byte,
            // which is not a rejection rule, it is a spelling preference.
            if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/.test(text)) {
                return { error: 'control-chars' };
            }
            return { text: text };
        }

        // The single entry point. `req` is {hostId, sid, winId, lastInputAt} —
        // captured by the caller at REQUEST time and immutable from here on, so
        // an async resolution can never re-read a front window that has since
        // changed. Returns nothing; the handler always reports handled.
        function osc52Request(req, data) {
            const semi = String(data).indexOf(';');
            if (semi === -1) return;                  // malformed: no Pc;Pd
            const pc = String(data).slice(0, semi);
            const pd = String(data).slice(semi + 1);

            // The query form, checked FIRST — ahead of the Pc filter, because a
            // read attempt is a read attempt whichever selection it names, and
            // `p;?` would otherwise be dropped by the Pc filter without the user
            // ever hearing that something tried to exfiltrate their clipboard.
            // Refused here, in code — never behind a setting.
            if (pd === '?') {
                // The ONLY sticky notice this policy raises — see the note on
                // the refusal notices below for why that matters.
                _osc52Notice('query', req,
                    'blocked: ' + _osc52Who(req) + ' tried to READ your '
                    + 'clipboard. Browserland never answers that request.',
                    { sticky: true, type: 'error' });
                return;
            }
            // Pc is a LIST, not a scalar: zero or more of `c s p q 0-7`, and an
            // empty Pc means s0. tmux's copy-mode copy really does emit
            // `ESC]52;;<base64>BEL` with Pc EMPTY (captured under
            // TERM=xterm-256color), so a strict equality check against
            // {c,s,""} would reject the single most common real emitter.
            //
            // Validated against the LEGAL ALPHABET first, then searched. A bare
            // substring test would accept `c ` — and a non-ASCII byte in
            // Pc is exactly what slips a sequence past the agent-side ring
            // strip, whose body class stops at \xc2. Rejecting a malformed Pc
            // here is both spec-correct and the thing that makes that gap
            // harmless.
            if (!/^[cspq0-7]*$/.test(pc)) return;
            // A request naming neither c nor s (`p`, `q`, `3`) is IGNORED: the
            // browser has one clipboard and no X11 PRIMARY to write, so
            // honouring a p-only request would let a selection the user aimed
            // at PRIMARY overwrite the CLIPBOARD they did not. Consequence,
            // disclosed rather than discovered: Neovim maps `+` to c and `*` to
            // p, so `"+y` works and `"*y` is ignored.
            if (pc !== '' && pc.indexOf('c') === -1 && pc.indexOf('s') === -1) {
                return;
            }
            if (pd === '') return;                    // clear-selection: see below

            // GATE 1 — per-host authorization. First refusal per host per page
            // load says so out loud: learning that the TUI tried to copy, and
            // where the switch is, is the other half of the original bug.
            if (!isOsc52Allowed(req.hostId)) {
                // Keyed exactly the way the GRANT is, so the prompt and the
                // switch it points at can never name different machines. Timed
                // rather than burned once per page load: a program that emits
                // one OSC 52 at startup would otherwise consume the only offer,
                // and the user's own copy ten minutes later would fail in the
                // very silence this feature exists to remove.
                const hk = _osc52HostKey(req.hostId) || String(req.hostId);
                const at = Date.now();
                if (at - (_osc52.offeredAt.get(hk) || 0) >= OSC52_REOFFER_MS) {
                    _osc52.offeredAt.set(hk, at);
                    // Sticky, because it is ACTIONABLE and the user should not
                    // have four seconds to catch it. Named by HOST, not by
                    // terminal, so showNotice's identical-text dedupe collapses
                    // every terminal on that host into one: per-terminal text
                    // would let a broker with many sessions mint many distinct
                    // stickies and push the `?` read warning out of the cap.
                    showNotice(
                        'A program on ' + _osc52Where(req) + ' tried to set '
                        + 'your clipboard. Allow it in Control Panel → that '
                        + 'host’s tab → Clipboard (OSC 52).', { sticky: true });
                }
                return;
            }
            // GATE 2 — never write to a backgrounded tab.
            if (!document.hasFocus()) return;
            // GATE 3 — front terminal only.
            if (frontId !== req.winId) return;
            // GATE 4 — recent user input in THAT window.
            if (!(Date.now() - (req.lastInputAt || 0) < OSC52_ACTIVITY_MS)) {
                _osc52Notice('stale', req,
                    'clipboard write from ' + _osc52Who(req)
                    + ' ignored (no recent activity in that terminal)');
                return;
            }

            const res = _osc52Decode(pd);
            if (res.error) {
                // Every rejection is visible. Distinct reasons get distinct
                // advice; they are not interchangeable.
                //
                // None of them are STICKY, on purpose. showNotice caps sticky
                // notices at STICKY_NOTICE_MAX and drops the OLDEST, so a burst
                // of refusals with distinct reasons — each one dodging the
                // per-reason throttle — could push the `?` read warning off the
                // screen within a single parse pass, and take other subsystems'
                // sticky notices with it. The read warning is the one thing
                // here that must survive, so it is the only sticky one.
                const who = _osc52Who(req);
                const msg = {
                    'too-large': 'clipboard copy from ' + who + ' refused: over '
                        + '1 MiB. Nothing was copied (it is not truncated).',
                    'not-utf8': 'clipboard copy from ' + who + ' refused: the '
                        + 'text is not valid UTF-8.',
                    'control-chars': 'clipboard copy from ' + who + ' refused: '
                        + 'it contains control characters.',
                    'bad-base64': 'clipboard copy from ' + who + ' refused: '
                        + 'malformed payload.',
                }[res.error];
                // 'empty' is a no-op, not a refusal — see the deviation note
                // below; it needs no notice.
                if (msg) {
                    _osc52Notice(res.error, req, msg, { type: 'error' });
                }
                return;
            }

            // GATE 5 — rate limit, browser-global.
            const now = Date.now();
            _osc52.accepted = _osc52.accepted.filter(
                t => now - t < OSC52_RATE_WINDOW_MS);
            if (_osc52.accepted.length >= OSC52_RATE_MAX) {
                _osc52Notice('rate', req,
                    'clipboard writes from ' + _osc52Who(req)
                    + ' are coming too fast — ignoring for a moment.',
                    { type: 'error' });
                return;
            }

            if (!canWriteClipboardModern()) {
                // Distinct from a permission denial: the API is not merely
                // refusing, it does not exist on this origin, and the fix is a
                // different one (https or localhost, e.g. `tailscale serve`).
                _osc52Notice('insecure', req,
                    'clipboard copy from ' + _osc52Who(req) + ' needs a secure '
                    + 'origin — open Browserland over https or on '
                    + 'localhost.', { type: 'error' });
                return;
            }
            _osc52.accepted.push(now);
            // Deliberately NOT copyTextToClipboard: its copyTextLegacy fallback
            // stashes a hidden textarea and calls .select(), stealing focus and
            // able to abort an in-flight IME composition. That is the right
            // fallback for a user-initiated copy inside a gesture; it is the
            // wrong one for an unprompted write driven by PTY output.
            navigator.clipboard.writeText(res.text).then(() => {
                // The focus and front-terminal gates were evaluated BEFORE this
                // promise, which settles later — by now the front window may
                // have changed. Re-checking here would be theatre (the write has
                // already landed), so the fix is the other one the race allows:
                // the notice reports the identity captured at REQUEST time, so
                // it names the terminal that actually asked rather than whatever
                // happens to be in front when the promise resolves.
                _notifyClipboard('out', res.text);    // #106 history
                showNotice('copied ' + res.text.length + ' chars from '
                    + _osc52Who(req));
            }).catch(() => {
                _osc52Notice('denied', req,
                    'clipboard copy from ' + _osc52Who(req)
                    + ' was blocked by the browser.', { type: 'error' });
            });
        }
        // Deviation from xterm, stated plainly: xterm treats an empty OR
        // non-base64 Pd as "clear the selection". We do not clear. Silently
        // wiping a user's clipboard because a program printed a malformed
        // sequence is worse than doing nothing, and "a hostile program can
        // erase your clipboard" is a nuisance primitive we decline to build.

        // ---- auth -----------------------------------------------------------
        // The password IS the broker's auth_token — one secret per broker,
        // kept per-browser in localStorage (each host record's token slot).
        // ?token=/?auth= in the URL is still accepted for deep links:
        // adopted into the local host's slot, then scrubbed via
        // history.replaceState (?session= survives). No cookies, no /login.
        const params = new URLSearchParams(window.location.search);
        (function adoptUrlToken() {
            const t = params.get('token') || params.get('auth');
            if (!t) return;
            localHost().token = t;
            savePrefs();
            try {
                const u = new URL(window.location.href);
                u.searchParams.delete('token');
                u.searchParams.delete('auth');
                history.replaceState(null, '', u);
            } catch (_) {}
        })();

        // Every fetch / WS dial goes through these: same-origin for the local
        // host, the stored origin for remotes, each host's own token attached.
        //
        // HTTP sends the token as `Authorization: Bearer`, never in the query
        // string (#144). A URL credential leaks everywhere a header does not:
        // performance.getEntriesByType('resource') hands the full URL to ANY
        // script on the page, DevTools HAR exports carry it into bug reports,
        // and a reverse proxy (`tailscale serve`, the recommended topology)
        // logs it. There is deliberately NO function here that builds a
        // token-bearing HTTP URL, so one cannot be reintroduced by accident.
        //
        // Cost: an Authorization header makes a cross-origin request
        // non-simple, so remote hosts now pay an OPTIONS preflight. The broker
        // answers every one and sets Access-Control-Max-Age: 86400, so it is
        // one round trip per host per day, not per request. ACAO stays `*`:
        // a header set explicitly by fetch() is NOT a "credentialed" request in
        // the CORS sense (that needs credentials:'include'), so `*` remains
        // legal and a wrong token still returns a READABLE 401 rather than an
        // opaque failure — which is what keeps the login overlay working.
        function hostUrl(host, path) {
            return ((host && host.url) ? host.url : '') + path;
        }
        function hostFetch(host, path, opts) {
            const o = Object.assign({}, opts || {});
            // Headers(), NOT Object.assign: a caller passing a real Headers
            // instance (or an array of pairs) would spread to {} and SILENTLY
            // drop its Content-Type, turning a JSON POST into a bodyless one
            // the broker then rejects as bad_json. No caller does that today;
            // this makes it impossible for one to start. Headers() also
            // replaces any existing authorization case-insensitively.
            const headers = new Headers(o.headers || undefined);
            if (host && host.token) {
                headers.set('Authorization', 'Bearer ' + host.token);
            }
            o.headers = headers;
            // Every request carries a DEADLINE, because this is the one door
            // every HTTP call in the UI walks through. A host that is merely
            // OFF refuses the connection and fails fast; a host that is
            // black-holed (asleep, dropped off the tailnet, a firewall that
            // silently discards) does not, and the browser will sit on that
            // connect for ~90 s — every await behind it (the boot /state pull,
            // the 2 s taskbar poll) stalls with it, which is what "the whole
            // interface locks up while it checks the server" actually is.
            // Default FETCH_TIMEOUT_MS; opts.timeoutMs overrides it for a call
            // that is legitimately slower (an upload, a 30 s paste). Pass
            // timeoutMs: 0 (or any non-positive / non-finite value) to opt OUT
            // entirely — for a caller that owns a deadline of its own, e.g. one
            // spanning the body read as well (fetch() resolves on HEADERS, so
            // the deadline below is disarmed the moment they land — a slow body
            // is never guillotined mid-stream, and a caller that cares about a
            // stalled body must time the read itself).
            // timeoutMs is OURS, not a fetch init key: drop it before the
            // request sees it rather than trusting fetch to ignore it.
            const timeoutMs = (o.timeoutMs === undefined)
                ? FETCH_TIMEOUT_MS : o.timeoutMs;
            delete o.timeoutMs;
            if (!(typeof timeoutMs === 'number' && isFinite(timeoutMs)
                    && timeoutMs > 0)) {
                return fetch(hostUrl(host, path), o);
            }
            // COMPOSE with the caller's signal, never replace it: a caller that
            // cancels (a closed progress dialog, an abandoned upload) must still
            // abort this request, and with ITS reason — an AbortError the UI
            // reads as "the user cancelled" — while our own abort carries a
            // TimeoutError so the two stay distinguishable. AbortSignal.any()
            // would be the one-liner, but it is far newer than the browser floor
            // the rest of this file codes to (see the crypto.randomUUID fallback
            // in 50_js_constants.js), so compose by hand. Older engines ignore
            // abort()'s reason argument and synthesize a plain AbortError; that
            // is a graceful degradation, not worth a shim.
            const callerSignal = o.signal;
            const ctrl = new AbortController();
            o.signal = ctrl.signal;
            const timer = setTimeout(() => {
                ctrl.abort(new DOMException('timeout', 'TimeoutError'));
            }, timeoutMs);
            let onCallerAbort = null;
            if (callerSignal) {
                if (callerSignal.aborted) {
                    ctrl.abort(callerSignal.reason);
                } else {
                    onCallerAbort = () => ctrl.abort(callerSignal.reason);
                    callerSignal.addEventListener('abort', onCallerAbort);
                }
            }
            return fetch(hostUrl(host, path), o).finally(() => {
                clearTimeout(timer);
                // A caller signal can outlive many requests (one controller per
                // upload, per progress dialog): drop our listener or they pile
                // up on it for as long as it lives.
                if (onCallerAbort) {
                    callerSignal.removeEventListener('abort', onCallerAbort);
                }
            });
        }
        function hostWsUrl(host, path) {
            let base;
            if (host && host.url) {
                base = host.url.replace(/^http/, 'ws');
            } else {
                const proto = window.location.protocol === 'https:'
                    ? 'wss:' : 'ws:';
                base = proto + '//' + window.location.host;
            }
            // The ONE place a token still rides a URL, and it is unavoidable:
            // the browser WebSocket API cannot set request headers on the
            // handshake. /ws, /control and /browserland therefore keep
            // ?token=. Documented in docs/TECHNICAL.md rather than papered
            // over — closing it needs a connect-ticket scheme, not a refactor.
            if (!host || !host.token) return base + path;
            return base + path + (path.indexOf('?') === -1 ? '?' : '&')
                + 'token=' + encodeURIComponent(host.token);
        }

        // Login overlay, per host: shown on any 401/4401 — first visit
        // without a password, or a stale one after that broker restarted
        // with a new token. Auto-pops at most ONCE per failing host
        // (authPrompted); after a cancel the amber "auth" taskbar chip is
        // the way back in — never a re-popping modal loop.
        const authOverlay = document.getElementById('auth-overlay');
        const authErrorEl = document.getElementById('auth-error');
        const authTokenEl = document.getElementById('auth-token');
        const authHostLine = document.getElementById('auth-host-line');
        const authSubmitBtn = document.getElementById('auth-submit');
        let authOverlayHostId = null;   // host the open overlay belongs to
        const authPrompted = new Set(); // host ids already auto-popped
        let authSubmitting = false;     // a probe is in flight (double-submit guard)
        // Bumped every time the overlay opens on a host or closes. The submit
        // handler captures it, so a probe that resolves AFTER the user hit Cancel
        // (or after the overlay was forced onto another host) can no longer store
        // a token, hide a dialog it doesn't own, or re-authorise a dismissed
        // prompt. A slow broker + an impatient Cancel used to do exactly that.
        let authGeneration = 0;
        function showAuthOverlay(host, force) {
            if (!host) return;
            if (authOverlay.classList.contains('open')) {
                // Never clobber a form the user is typing into.
                if (authOverlayHostId === host.id || !force) return;
            } else if (!force && authPrompted.has(host.id)) {
                return;                  // degraded to the taskbar chip
            }
            authPrompted.add(host.id);
            authOverlayHostId = host.id;
            authGeneration++;            // stales any probe from a previous open
            // textContent only — labels are user input and tokens live in
            // localStorage; never innerHTML a user-controlled string.
            authHostLine.textContent = host.id === 'local'
                ? 'this broker (' + window.location.host + ')'
                : host.label + ' (' + host.url + ')';
            authErrorEl.classList.remove('show');
            authTokenEl.value = '';
            authOverlay.classList.add('open');
            try { authTokenEl.focus(); } catch (_) {}
        }
        function hideAuthOverlay() {
            authOverlay.classList.remove('open');
            authOverlayHostId = null;
            authGeneration++;            // a probe still in flight is now stale
        }
        document.getElementById('auth-cancel').addEventListener('click', () => {
            hideAuthOverlay();
            // The amber chip must render even for a cancelled single-host
            // local login, or the UI is bricked with no way back in.
            renderHostStatus();
        });
        document.getElementById('auth-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const host = hostById(authOverlayHostId);
            if (!host) { hideAuthOverlay(); return; }
            const candidate = authTokenEl.value;
            if (!candidate) return;
            if (authSubmitting) return;   // Enter + click: one probe, not two
            const gen = authGeneration;   // see authGeneration above
            let resp;
            authSubmitting = true;
            const prevLabel = authSubmitBtn ? authSubmitBtn.textContent : '';
            if (authSubmitBtn) {
                authSubmitBtn.disabled = true;
                authSubmitBtn.textContent = 'checking…';
            }
            try {
                // Probe with the CANDIDATE token, not the stored one, via the
                // same Bearer path every other request uses (#144). It must
                // not be a ?token= URL: the broker reads the query BEFORE the
                // header, so a leftover query token would silently win over
                // the header and make a correct password look wrong -- and the
                // probe URL would land in Resource Timing like any other.
                resp = await hostFetch({ url: host.url, token: candidate },
                                       '/sessions');
            } catch (err) {
                if (gen !== authGeneration) return;   // dialog already dismissed
                // hostFetch's deadline surfaces as a TimeoutError DOMException.
                // It must NEVER read like a rejected password: a slow/absent
                // broker is not a wrong secret, and telling the user otherwise
                // sends them hunting for a token that was right all along.
                // Anything else here (a TypeError, with CORS pinned onto 401s
                // too) really is "host down".
                authErrorEl.textContent =
                    (err && err.name === 'TimeoutError')
                        ? 'broker unreachable (timed out)'
                        : 'broker unreachable: ' + err;
                authErrorEl.classList.add('show');
                return;
            } finally {
                // Always restored — including on a throw, a stale-generation
                // bail, or the success path below.
                authSubmitting = false;
                if (authSubmitBtn) {
                    authSubmitBtn.disabled = false;
                    authSubmitBtn.textContent = prevLabel;
                }
            }
            // The user cancelled (or the overlay moved to another host) while
            // the probe was out: do NOT reopen/re-authorise what they dismissed.
            if (gen !== authGeneration) return;
            if (resp.status === 401) {
                authErrorEl.textContent = 'invalid password';
                authErrorEl.classList.add('show');
                return;
            }
            if (!resp.ok) {
                authErrorEl.textContent = 'probe failed: HTTP ' + resp.status;
                authErrorEl.classList.add('show');
                return;
            }
            host.token = candidate;
            savePrefs();
            authPrompted.delete(host.id);   // future failures may pop again
            const st = hostPolls.get(host.id);
            if (st) st.authNeeded = false;
            authTokenEl.value = '';
            hideAuthOverlay();
            renderHostStatus();
            refreshTaskbar();
            // Re-dial this host's control WS (the single-active lease channel):
            // it may have died on a 4401 before the token was entered. For the
            // HOME broker this is what un-blocks the deferred boot / overlay.
            try { openControlWs(host); } catch (_) {}
            // #157: the boot /info was 401 for a browser that had no token yet,
            // so this broker's MOD POLICY was unknown and every mod came up at
            // this browser's own default. Now that we can ask, re-ask once and
            // reconcile. A no-op for a normal (already-authenticated) load and
            // for every remote host — see notifyModsHostAuth in 86.
            try { notifyModsHostAuth(host.id); } catch (_) {}
            // App windows (text editor / file manager) carry hostId 'app', so
            // the terminal-healing loop below never reaches them. Notify any
            // file tool bound to the host that just authenticated so it can
            // refresh its view in place rather than wait for a manual retry
            // (#46). Wrapped per-window so one bad hook can't break the rest.
            for (const win of windows.values()) {
                if (win.disposed || !win._onHostAuth) continue;
                try { win._onHostAuth(host.id); } catch (_) {}
            }
            // Heal in place: re-dial THIS host's windows that died on auth
            // (or are just dead) — the relay re-snapshots on attach.
            for (const win of windows.values()) {
                if (win.disposed || win.hostId !== host.id) continue;
                if (!win.wsOpen || win.authFailed) {
                    win.authFailed = false;
                    win.reattachAttempts = 0;
                    win.reattachAt = 0;
                    reattachWindow(win);
                }
            }
        });

        // ---- single-active-browser overlay (F-ACTIVECLIENT) ---------------
        // Full-page takeover prompt shown when the HOME broker reports another
        // browser holds the lease. The only path back in is the button, which
        // sends become_active; the view rebuilds ONLY on the broker's
        // {active:true} push (no optimistic UI).
        const becomeActiveOverlay =
            document.getElementById('become-active-overlay');
        function showBecomeActiveOverlay() {
            if (becomeActiveOverlay) becomeActiveOverlay.classList.add('open');
        }
        function hideBecomeActiveOverlay() {
            if (becomeActiveOverlay) {
                becomeActiveOverlay.classList.remove('open');
            }
        }
        const becomeActiveBtn = document.getElementById('become-active-btn');
        if (becomeActiveBtn) {
            becomeActiveBtn.addEventListener('click', () => {
                // Request only — wait for the broker's {active:true} to rebuild.
                sendBecomeActive(localHost());
            });
        }

        // ---- default / locked size ------------------------------------------
        // One size drives both: the Control Panel cols×rows value (when set) is
        // converted to pixels through the measured cell box plus the
        // .term-window chrome constants (+12 horiz, +38 vert — see the
        // box-model comment in the `resized` handler). With no setting the
        // legacy 720×480 px default applies unchanged.
        function liveCellDims() {
            for (const win of windows.values()) {
                if (win.disposed) continue;
                const dims = readCellDims(win);
                if (dims) return dims;
            }
            return getSettings().cellDims;   // last measured, persisted
        }
        function defaultPixelSize() {
            const size = getSettings().size;
            if (size) {
                const dims = liveCellDims();
                if (dims) {
                    return {
                        width: Math.max(MIN_W, Math.ceil(size.cols * dims.w) + 12),
                        height: Math.max(MIN_H, Math.ceil(size.rows * dims.h) + 38),
                    };
                }
            }
            return { width: DEFAULT_W, height: DEFAULT_H };
        }

        // ---- size lock ----------------------------------------------------
        // Reserved key inside the same prefs object — numeric session ids
        // never collide with the leading underscore.
        function isSizeLocked() { return !!prefs._lockSize; }
        function lockedSize() { return defaultPixelSize(); }
        function applyLockedSizeToAll() {
            // Snap every live window to the locked size at its current
            // position. applyGeomToWindow's internal lock guard makes
            // this idempotent.
            const ls = lockedSize();
            for (const win of windows.values()) {
                if (win.disposed || win.tiled) continue;   // tiled: flex-sized
                const g = win.geom || {};
                applyGeomToWindow(win, {
                    left: g.left | 0,
                    top: g.top | 0,
                    width: ls.width,
                    height: ls.height,
                });
                refitSoon(win);
            }
            savePrefs();
        }
        function setSizeLocked(on) {
            prefs._lockSize = !!on;
            savePrefs();
            document.body.classList.toggle('size-locked', !!on);
            if (on) applyLockedSizeToAll();
            // Unlock leaves windows at their current (locked) size — user
            // can drag-resize or run a tile action to redistribute.
        }

