        // ---- ctx.http: the sanctioned authenticated fetch (#200) ------------
        //
        //     const r = await ctx.http.fetch(hostId, '/info', {
        //         method: 'GET',   // default
        //         json,            // request body; sets content-type
        //         timeoutMs,       // TOTAL deadline — INCLUDING the body read
        //         signal,          // composes with #198's ctx.signal
        //     });
        //     // -> {status, json?|text?, error?}   NEVER rejects.
        //
        // WHAT IT REPLACES. Mods doing authenticated HTTP call the raw hoisted
        // hostFetch (63), which has two footguns the mods themselves document:
        //
        //   1. A NULL HOST SILENTLY TARGETS THE SAME ORIGIN. hostUrl(host, path)
        //      is `((host && host.url) ? host.url : '') + path`, so a lookup
        //      that came back null — a removed host, a typo'd id, a stale
        //      cached id — issues the request against THIS broker instead of
        //      failing. recorder's own header comment (:105-111) writes the
        //      footgun down rather than being able to fix it.
        //   2. ITS DEADLINE STOPS AT HEADERS. fetch() resolves the moment the
        //      status line lands, and hostFetch's own comment says so: "the
        //      deadline below is disarmed the moment they land — a slow body is
        //      never guillotined mid-stream, and a caller that cares about a
        //      stalled body must time the read itself". mod-sync hand-rolls an
        //      AbortController around exactly that (mod-sync.js:485-495). One
        //      correct pattern, re-typed per mod, per route.
        //
        // Both die here, and NEITHER is fixed in hostFetch itself: core keeps
        // it unchanged (#200's out-of-scope list). This is a wrapper, the same
        // shape ctx.file and ctx.session already are.
        //
        // ITS OWN FRAGMENT, for the reason 86a/86b/86d/86e/86f/86g/86h each got
        // one: 86c_js_mod_ctx_ext.js is at the #68 2500-line per-fragment cap
        // (_MAX_LINES, ui.py) and the rule for that cap has always been "split,
        // never trim". Ordered among the 86* companions and still before the
        // mod-script splice, because a mod's init reads the finished ctx. One
        // <script>, one scope: this file calls 63's hostFetch, 56's hostById
        // and 86c's _registerCtxExtender / _registerModCapability directly.
        //
        // THE DECISIONS #200 MADE, implemented here rather than rediscussed:
        //
        //  1. hostId IS REQUIRED, and a bad one THROWS SYNCHRONOUSLY. The
        //     same-origin default dies at the door: there is no ''/'local'/
        //     undefined fallback branch of the kind _modFileHost, _modSessionHost
        //     and _modStoreHost each carry. Targeting the origin broker is
        //     SPELLED: pass the id of the `self: true` entry from
        //     ctx.hosts.list(). A synchronous throw, not a resolved error,
        //     because passing no host is a program defect at the call site —
        //     the stack points at the line, and a mod that swallows every
        //     result shape cannot swallow this one into a same-origin write.
        //  2. AN UNKNOWN hostId FAILS CLOSED WITH NO REQUEST ISSUED —
        //     {status: 0, error: 'host_not_found'} — the contract ctx.file
        //     keeps (_modFileApi's synthetic host_not_found). It is a resolved
        //     value, not a throw, because an id going stale is DATA (the user
        //     deleted a host between a poll and its retry), not a typo.
        //  3. THE DEADLINE IS TOTAL. See _modHttpFetch's timer below: hostFetch
        //     is called with timeoutMs: 0 — deliberately DISARMING its
        //     headers-only deadline — and this file's own controller stays
        //     armed across `r.text()`. A route that sends its status line and
        //     then stalls the body aborts at roughly timeoutMs instead of
        //     hanging forever.
        //  4. NO AUTH SIDE EFFECTS. A 401 comes back AS a 401. Nothing in this
        //     file calls promptFileHostAuth, touches authPrompted, or opens the
        //     auth overlay — a background poll popping a modal was the #161 bug
        //     class. Auth recovery belongs to the mod, and later to #195's
        //     `host:auth` event.
        //
        // STATUS IS ALWAYS PRESENT, AND `error` NEVER SUBSTITUTES FOR ONE. When
        // a complete answer arrives, `status` is its HTTP status and there is
        // no `error` key at all — so a 401 with an empty body ({status: 401,
        // text: ''}) and an empty 200 ({status: 200, text: ''}) stay different
        // objects. ctx.serverStore.get once dropped the status and they were
        // not (the #174 fix). `status: 0` + `error` is reserved for the cases
        // where there is NO answer to report: an unresolved host, a transport
        // failure, a cancel, a deadline.
        //
        // A ROUTE THE PEER DOES NOT HAVE IS {status: 0, error} AND THAT IS
        // CORRECT, not a gap. A new route is invisible to an old peer: the
        // cross-origin preflight OPTIONS 404s and the browser surfaces an
        // opaque network TypeError with no status behind it (#157). There is
        // no status to invent, so it arrives as a branchable status-0 error —
        // which is precisely what cross-broker callers feature-detect on. They
        // never assume the peer has the route.
        //
        // A BODY THAT FAILED TO ARRIVE IS status 0, EVEN THOUGH THE HEADERS
        // CAME. It is the one place the two rules above meet, so it is stated:
        // a stalled body that hits the deadline mid-stream resolves
        // {status: 0, error: 'TimeoutError: timeout'}, NOT {status: 200} with a
        // truncated string. A caller branching on 200 would be branching on an
        // answer it does not have. Whenever the answer IS complete, its status
        // is reported verbatim and `error` is absent — the rule that keeps 401
        // and empty apart is untouched by this one.
        //
        // A MID-FLIGHT REPOINT CANNOT REDIRECT AN IN-FLIGHT CALL. The host is
        // resolved ONCE, before the request, and the OBJECT is handed to
        // hostFetch, which reads its url and token at request-build time. An
        // edit to prefs._hosts after that affects the NEXT call only; there is
        // no retry in this file that could re-resolve and land on a new
        // endpoint under the old call's expectations. (`brokerId` identity is
        // ctx.hosts.list()'s business, not this one's — #200 splits them
        // deliberately, and nothing here reasons from a URL.)
        //
        // A NEW CAPABILITY ENTRY IS OWED, and this is the argument: #197's map
        // is per FAMILY — a bare top-level ctx member name, never a dotted path
        // — and `http` is a NEW top-level member of the ctx, exactly like
        // #195's `hosts`, #198's `signal` and #199's `commands`. (#196's
        // saveChain owed none: it decorates `serverStore`, a member of a family
        // that already has an entry.) Without the registration ctx.capabilities
        // would lie by omission about surface the build demonstrably has.
        // ctxVersion stays 1: additive, feature-detected surface
        // (`ctx.http && typeof ctx.http.fetch === 'function'`), and a bump
        // would refuse every mod that pins v1.
        //
        // NO SURFACE ON A BUILD THAT CANNOT CARRY IT (the CP8 no-half-family
        // rule). An engine with no constructible AbortController cannot honor
        // the total deadline, which is the whole reason this family exists —
        // so it gets NO ctx.http rather than a fetch whose timeoutMs silently
        // does nothing. ctx.capabilities then reports it absent, which is true,
        // and `needs: ['http']` blocks the mod with a reason.

        // The default deadline. Sits above _modSessionApi's 12 s (which is
        // pinned just over the server's 10 s RPC wait) because a /file or
        // /update route is legitimately slower than a session RPC, and far
        // below the ~90 s a black-holed host would otherwise cost. A caller
        // that owns a longer transfer passes its own timeoutMs; timeoutMs: 0
        // (or any non-positive / non-finite value) opts out entirely, the same
        // spelling hostFetch already uses — an opt-out here is genuinely
        // unbounded, since there is no headers-only fallback left underneath.
        const MOD_HTTP_TIMEOUT_MS = 15000;

        // Argument errors name the mod, because this throw lands in whatever
        // called init() or a poll callback and the stack alone may not say
        // which mod. `rec.id` is an attacker-adjacent read (a proxy, a throwing
        // getter), so building the message must not become the failure.
        function _modHttpArgError(rec, msg) {
            let who = '';
            try { who = (rec && rec.id) ? ' (mod "' + rec.id + '")' : ''; }
            catch (_) { who = ''; }
            return new TypeError('ctx.http.fetch: ' + msg + who);
        }
        // A TimeoutError, not a bare abort, so a caller reading the string can
        // tell the deadline apart from its own cancel — the distinction
        // hostFetch already maintains, kept when its deadline is disarmed.
        // undefined on an engine with no constructible DOMException: abort()
        // then synthesizes a plain AbortError, a graceful degradation.
        function _modHttpTimeoutReason() {
            try { return new DOMException('timeout', 'TimeoutError'); }
            catch (_) { return undefined; }
        }
        // The one place a complete answer becomes a result object. JSON is
        // parsed when the CONTENT-TYPE says json and the parse succeeds;
        // everything else — including a body the peer mislabelled — comes back
        // as `text` for the caller to decide about. Deliberately not r.json()
        // (which would need a second read to recover the text) and deliberately
        // not "parse anything that parses" (which turns a plain-text `42` into
        // a number). `status` is written FIRST and never from the body: a body
        // carrying its own `status` — a hostile or simply odd peer, and the
        // store is read across brokers since #174 — must not be able to
        // overwrite the transport status a caller is about to branch on.
        function _modHttpBody(status, ctype, text) {
            const out = { status: status };
            const isJson = typeof ctype === 'string'
                && ctype.toLowerCase().indexOf('json') !== -1;
            if (isJson && text !== '') {
                try { out.json = JSON.parse(text); return out; }
                catch (_) { /* mislabelled body: fall through to text */ }
            }
            out.text = text;
            return out;
        }
        // The call itself. Everything before the first `return` is validation
        // that THROWS: a missing host, a relative path, a body on a bodyless
        // method, an unserializable body. All four are defects at the call
        // site, not outcomes — and each is the specific mistake that, left to
        // resolve as a value, would end up silently issuing a DIFFERENT request
        // than the caller meant. Everything after the validation resolves, and
        // nothing rejects.
        function _modHttpFetch(rec, hostId, path, opts) {
            const o = opts || {};
            if (typeof hostId !== 'string' || hostId === '') {
                throw _modHttpArgError(rec,
                    'hostId is required — pass the id of an entry from '
                    + 'ctx.hosts.list() (the `self: true` one is this broker); '
                    + 'there is no same-origin default');
            }
            if (typeof path !== 'string' || path.charAt(0) !== '/') {
                throw _modHttpArgError(rec,
                    'path must be an absolute route beginning with "/"');
            }
            // ...and NOT an authority. `hostUrl` is `(host.url || '') + path`,
            // and the SELF entry's url is '' — so a path of `//evil/x` is not a
            // route on this broker, it is the protocol-relative URL
            // `//evil/x`, which fetch resolves to another ORIGIN. hostFetch
            // then attaches `Authorization: Bearer <that host's token>` to it.
            // A mod building a path out of anything it did not write itself (a
            // route name from a server response, a saved recording's id) would
            // hand this broker's token to whoever chose the string. The whole
            // point of this family is that `hostId` selects the target, so a
            // `path` that can re-select it is not a footgun to document — it is
            // the one the wrapper exists to remove. Backslashes too: several
            // engines normalise `/\` to `//` before parsing.
            // ...and no C0 control anywhere in it. This is not belt-and-braces:
            // the URL parser REMOVES every ASCII tab, LF and CR from its input
            // BEFORE parsing, so `/<TAB>/evil/x` has a second character that is
            // not a slash — it passes a charAt(1) test — and then parses as
            // `//evil/x`, i.e. the very authority the check below refuses.
            // Rejecting the whole C0 range costs nothing (a control character
            // has no business in an unencoded route; %09 still works) and does
            // not depend on remembering which three the parser happens to
            // strip. Checked BEFORE the authority test, because the point is
            // that the string the parser sees is not the string we validated.
            for (let i = 0; i < path.length; i++) {
                const cc = path.charCodeAt(i);
                if (cc <= 0x1f || cc === 0x7f) {
                    throw _modHttpArgError(rec,
                        'path must not contain control characters: the URL '
                        + 'parser strips tab/LF/CR before parsing, so they can '
                        + 'smuggle an authority past every check');
                }
            }
            if (path.charAt(1) === '/' || path.charAt(1) === '\\') {
                throw _modHttpArgError(rec,
                    'path must be a route, not an authority: "'
                    + path.slice(0, 12) + '" would target another origin — '
                    + 'the host is chosen by hostId, never by the path');
            }
            const method = (typeof o.method === 'string' && o.method)
                ? o.method.toUpperCase() : 'GET';
            const hasBody = (o.json !== undefined);
            if (hasBody && (method === 'GET' || method === 'HEAD')) {
                throw _modHttpArgError(rec,
                    'a ' + method + ' cannot carry `json` — name the method');
            }
            let body = null;
            if (hasBody) {
                try { body = JSON.stringify(o.json); }
                catch (e) {
                    throw _modHttpArgError(rec,
                        '`json` is not serializable: ' + e);
                }
                // stringify does not always THROW on unserializable input: a
                // function, a symbol, or an object whose toJSON() returns
                // undefined all yield `undefined` quietly. Sending that would
                // put a bodyless request on the wire under a json content-type
                // — the broker answers bad_json and the caller reads it as a
                // server fault rather than the call-site defect it is.
                if (body === undefined) {
                    throw _modHttpArgError(rec,
                        '`json` serialized to nothing (a function, a symbol, '
                        + 'or a toJSON returning undefined)');
                }
            }
            // FAIL CLOSED, and note what is NOT here: no ''/'local'/undefined
            // fallback to localHost(). hostById resolves the local broker under
            // its real id ('local', entry 0 of getHosts), so the self entry
            // from ctx.hosts.list() routes through the same lookup every remote
            // does. An id nobody knows resolves to no request at all.
            const host = hostById(hostId);
            if (!host) {
                return Promise.resolve({ status: 0, error: 'host_not_found' });
            }
            const timeoutMs = (o.timeoutMs === undefined || o.timeoutMs === null)
                ? MOD_HTTP_TIMEOUT_MS : o.timeoutMs;
            // CLAMPED to the largest delay a timer can actually represent.
            // setTimeout's delay is a signed 32-bit int: hand it 2**31 and it
            // overflows to a negative, which fires on the NEXT TICK. A caller
            // asking for a 25-day deadline would get one of about zero
            // milliseconds — the exact opposite of what they wrote, and
            // indistinguishable from a broker that answered instantly.
            const MAX_DELAY = 2147483647;
            let deadline = (typeof timeoutMs === 'number'
                && isFinite(timeoutMs) && timeoutMs > 0) ? timeoutMs : 0;
            if (deadline > MAX_DELAY) deadline = MAX_DELAY;
            // COMPOSE the caller's signal, never replace it — the rule hostFetch
            // already follows, restated here because this file owns the
            // controller now. An ALREADY-ABORTED caller signal (the common
            // shape once ctx.signal is threaded through: the mod was torn down
            // between scheduling the poll and running it) aborts ours before
            // fetch is ever reached, so no request is issued and the result is
            // the cancel, not a stray request from a dead activation.
            const ctrl = new AbortController();
            const caller = o.signal;
            let onCallerAbort = null;
            if (caller) {
                if (caller.aborted) {
                    ctrl.abort(caller.reason);
                } else {
                    onCallerAbort = function () { ctrl.abort(caller.reason); };
                    caller.addEventListener('abort', onCallerAbort);
                }
            }
            // THE TOTAL DEADLINE. The timer is NOT cleared when the headers
            // land — that is the entire difference from hostFetch, whose own
            // timer is cleared by the fetch promise settling. It is cleared in
            // `done()`, which runs only after the body has been read or has
            // failed, so a stalled body is aborted mid-stream and r.text()
            // rejects. hostFetch is therefore called with timeoutMs: 0, its
            // headers-only deadline deliberately disarmed: two live deadlines
            // would race, and the shorter (its) one would fire first and make
            // the total one unobservable.
            let timer = null;
            if (deadline) {
                timer = setTimeout(function () {
                    ctrl.abort(_modHttpTimeoutReason());
                }, deadline);
            }
            const done = function () {
                if (timer !== null) clearTimeout(timer);
                // A caller signal outlives many requests (one ctx.signal per
                // activation, hundreds of polls): drop the listener or they
                // pile up on it for as long as the activation lives.
                if (onCallerAbort) {
                    caller.removeEventListener('abort', onCallerAbort);
                }
            };
            const wantBlob = (o.blob === true);
            const init = { method: method, signal: ctrl.signal, timeoutMs: 0 };
            if (hasBody) {
                init.body = body;
                init.headers = { 'Content-Type': 'application/json' };
            }
            // hostFetch attaches the host's bearer token and nothing else — no
            // overlay, no authPrompted bookkeeping, no retry. A 401 is data.
            let p;
            try { p = hostFetch(host, path, init); }
            catch (e) { done(); return Promise.resolve(_modHttpFail(e)); }
            return p.then(function (r) {
                let ctype = null;
                try { ctype = r.headers.get('content-type'); }
                catch (_) { ctype = null; }
                const status = r.status;
                // `blob: true` reads the body as a Blob instead of text, and
                // exists for exactly one reason: SIZE. Reading a large body as
                // text decodes UTF-8 into a UTF-16 JS string, so a recording at
                // the broker's 256 MiB cap is held once as a string and again
                // as whatever the caller builds from it, where a Blob can be
                // spilled to disk by the browser and never fully resident. The
                // recorder download is the shipped case (its own comment chose
                // blob() for this before it moved onto this family). The
                // deadline still covers the read, and the result carries `blob`
                // in place of json/text -- no parsing, since the point is not
                // to touch the bytes.
                if (wantBlob) {
                    return r.blob().then(function (blob) {
                        done();
                        return { status: status, blob: blob };
                    }, function (e) {
                        done();
                        return _modHttpFail(e);
                    });
                }
                return r.text().then(function (text) {
                    done();
                    return _modHttpBody(status, ctype, text);
                }, function (e) {
                    done();
                    return _modHttpFail(e);
                });
            }, function (e) {
                done();
                return _modHttpFail(e);
            });
        }
        // Every no-answer outcome, in one shape: an unreachable host, a peer
        // that lacks the route (the opaque preflight failure of #157), a
        // cancel, a deadline. STRINGIFIED, like _modSessionApi's catch, because
        // a DOMException does not survive being passed around and the caller
        // wants a message it can log — the reason's NAME is what stays
        // branchable ('TimeoutError: timeout' vs 'AbortError: mod "x" was torn
        // down').
        function _modHttpFail(e) {
            // String(e) is not safe on its own: an abort reason whose
            // primitive conversion throws would turn the failure REPORT into a
            // rejection, from the function whose contract is that it never
            // rejects. The fallback names the shape rather than guessing.
            let msg;
            try { msg = String(e); }
            catch (_) { msg = 'request failed (unprintable error)'; }
            return { status: 0, error: msg };
        }
        // The extender. NO SURFACE ON A BUILD THAT CANNOT CARRY IT: without a
        // constructible AbortController the total deadline is unenforceable,
        // and without hostFetch there is no transport — either way a mod is
        // better told the family is absent than handed one that silently drops
        // its deadline.
        function _ctxHttp(ctx, rec) {
            if (typeof AbortController !== 'function') return;
            if (typeof hostFetch !== 'function') return;
            ctx.http = {
                fetch: function (hostId, path, opts) {
                    return _modHttpFetch(rec, hostId, path, opts);
                },
            };
        }
        // Guarded like every other registration in an extension fragment: a
        // page assembled without #194's registry or #197's capability map must
        // not throw at load.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxHttp);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('http', 1);
        }
        // ---- end ctx.http ---------------------------------------------------
