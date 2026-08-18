        // ---- mod: session recorder (#140) ---------------------------------
        // Record a live terminal session and play it back later at the ORIGINAL
        // recorded size. Capture rides ctx.windows.onTerminalCreate: a per-
        // terminal ⏺ title-bar button wraps THAT terminal's term.write (output)
        // and term.resize, and taps term.onData for input MARKERS only (byte
        // counts, never keystroke content — passwords are typed unechoed, so
        // recording input verbatim would capture secrets). Output is stored as
        // raw PTY bytes (base64) so playback is byte-faithful — no lossy
        // decode. A websocket swap mid-recording (reattach) records a 'g' gap
        // event; the snapshot redraw that follows is self-healing clear+redraw
        // content, so playback stays visually correct across gaps.
        //
        // Storage is BROKER-SIDE (#140 /recording/*): a finished recording
        // streams to the local broker via begin/chunk/commit (server-generated
        // ids) and lands as NDJSON — one meta line, then one line per event —
        // in webterm_recordings/ beside the state store. Durable: nothing is
        // TTL-swept; only the library window's delete removes one.
        //
        // #151 auto-record + segment roll: a Control Panel boolean
        // (recorder.autoRecord, default OFF, browser-global/synced) arms
        // capture on every terminal as it opens, and the 50 MiB ceiling now
        // ROLLS to a fresh segment instead of stopping — an always-on
        // recording must never silently end. Segments of one chain share a
        // `series` id and carry a 1-based `seg`, so the library can label the
        // parts; playback stays per-segment (each segment reseeds from a
        // snapshot, so it is independently watchable).
        //
        // Playback: the library window (the (+) menu entry) lists recordings;
        // Play opens an EPHEMERAL player window fixed at the recorded
        // cols×rows (no resize handles — the recording dictates the size,
        // following recorded resizes). Transport: play/pause, 0.25×–8× speed,
        // CONTINUOUS REVERSE (a real backwards animation), a scrubber with
        // note/gap markers, and timestamped notes (add/edit/delete) persisted
        // in a revision-checked sidecar. Reverse/seek re-render from the
        // nearest KEYFRAME: keyframes are built at LOAD time in an offscreen
        // terminal (awaiting xterm's write-callback flush before each
        // SerializeAddon snapshot, so they're exact at event boundaries) every
        // ~2s / ~192KiB of output. Without the serialize addon the player
        // degrades to replay-from-start seeks (correct, slower).
        registerMod({
            id: 'recorder',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: true,   // inert until ⏺ is clicked (or auto-record is opted into)
            tiers: ['window', 'settings'],
            // #200: EVERY byte this mod owns lives on a broker — capture saves
            // through /recording/begin|chunk|commit, the library lists over
            // /recordings, playback streams /recording?id=. So the scratchpad
            // argument applies and the theme/help one does not: the loss is
            // TOTAL, not partial. `needs` BLOCKS the mod, and blocked is the
            // better failure here — without ctx.http a capture would still arm,
            // fill memory for an hour and then have nowhere to go, which is
            // data loss dressed up as a working feature. Blocked means no ⏺ at
            // all, with a reason. (ctx.http is absent only on a build with no
            // constructible AbortController — 86i's no-half-family rule.)
            needs: ['http'],
            init: function (ctx) {
                if (!ctx.windows || !ctx.registerWindowKind) return;
                // Feature-detected as well as declared: the `needs` gate lives
                // in 86c, so a page assembled without that fragment has no gate.
                if (!ctx.http || typeof ctx.http.fetch !== 'function') return;

                const LIB_WIN_ID = 'app:recorder';
                // #151: both ceilings ROLL to a new segment (they used to stop).
                // BYTES bounds an output-heavy session; EVENTS bounds an output-
                // LIGHT one — input markers, resizes and gaps cost an array slot
                // each but no `bytes`, so a chatty-but-silent session would
                // otherwise grow the in-memory array with nothing to check.
                const REC_CAP_BYTES = 50 * 1024 * 1024;  // segment ceiling (output bytes)
                const REC_CAP_EVENTS = 250000;           // …and per-segment event count
                const KF_BYTES = 192 * 1024;             // keyframe every N output bytes…
                const KF_MS = 2000;                      // …or N ms of recorded time
                const CHUNK_RAW = 2 * 1024 * 1024;       // upload chunk (decoded)
                const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];

                const active = new Map();     // win.id -> recording state
                const disposers = new Set();  // live per-terminal UI teardowns
                let libRender = null;         // live library window repaint
                // #161: which broker the library lists ('' = all of them).
                // Module-scope, not per-window, so closing and reopening the
                // library keeps the filter — but it is deliberately NOT
                // persisted: a filter that survives a reload is a library that
                // silently hides recordings after you have forgotten you set it.
                let libFilterHostId = '';
                // #151 auto-record bookkeeping. `terms` is the registry the
                // setting's onChange walks to arm terminals that are ALREADY
                // open (onTerminalCreate only fires once per window, and it
                // already fired for them). `userStopped` is the per-window
                // override that keeps a ⏺ stop stopped — without it, nothing
                // stops auto-record from re-arming the terminal it was just
                // switched off on. `pendingSaves` counts uploads in flight so
                // the unload guard also covers the save, not just the capture.
                const terms = new Map();       // win.id -> {win, info, ui}
                const userStopped = new Set(); // win.ids where ⏺ stopped capture
                let pendingSaves = 0;

                // Default OFF = the key absent from the synced blob = today's
                // fully-manual behaviour. onChange fires on a local toggle AND
                // on a cross-browser /state convergence.
                const autoSetting = ctx.settings.boolean(
                    'recorder.autoRecord', false, {
                        title: 'Session recorder',
                        label: 'auto-record every session',
                        isBrowserGlobal: true,
                    });

                // ---- broker addressing (#161) ------------------------------
                // A recording lives on the broker that STORED it, and this page
                // can be attached to several. So every read/mutate call names
                // the broker it targets. Only the CAPTURE path stays pinned to
                // the local one: a session recorded in this browser is always
                // uploaded here, whichever broker the terminal itself came from
                // (so the origin chip below means "stored on", not "ran on").
                //
                // Callers carry a host ID, never a host OBJECT. The object can
                // be replaced (a prefs adopt from /state rebuilds the array) or
                // removed outright, so a captured one goes stale — holding a
                // token that has since been re-entered, or naming a broker that
                // is gone. Resolve at every call and treat "gone" as a visible
                // error. The old footgun this comment used to write down — a
                // null host falling back to the SAME ORIGIN, so a stale
                // reference quietly ran a delete against the local broker — is
                // GONE with #200: every route below goes through
                // ctx.http.fetch, where a missing id throws at the door and an
                // unknown one resolves {status: 0, error: 'host_not_found'}
                // with no request issued at all.
                const HOST_GONE = 'broker no longer configured';
                // The origin broker is now SPELLED, never implied: it is the
                // one `self: true` row of ctx.hosts.list() (#200). 'local' is
                // the id core mints for it (getHosts entry 0) and stays the
                // fallback for a build assembled without that member — and it
                // is also the id already written into every saved player
                // appData, so the two spellings agree by construction.
                function selfHostId() {
                    try {
                        if (ctx.hosts && typeof ctx.hosts.list === 'function') {
                            const rows = ctx.hosts.list();
                            for (const h of rows) {
                                if (h && h.self) return h.id;
                            }
                        }
                    } catch (_) { /* fall through to the minted id */ }
                    return 'local';
                }
                // Still a host OBJECT lookup, and deliberately: the only two
                // things left that want one are promptFileHostAuth, which takes
                // a host record, and the label. No fetch reads it.
                function recHost(hostId) {
                    return hostById(hostId || selfHostId());
                }
                function recHostLabel(host) {
                    if (!host) return 'broker';
                    return host.id === 'local' ? 'this broker'
                        : (host.label || host.url || host.id);
                }
                // Composite identity for anything keyed per recording: ids are
                // only unique WITHIN a broker. JSON rather than
                // `hostId + '|' + id` — host ids come out of a hand-editable
                // prefs blob, so no separator character is reserved.
                function recKey(hostId, id) {
                    return JSON.stringify([String(hostId || 'local'),
                                           String(id)]);
                }

                // ---- broker HTTP (ctx.http, #200: the token rides an
                // Authorization header, never the URL -- see hostFetch,
                // #144) -----------------------------------------------------
                // Per-route deadlines. These are all JSON control calls on a
                // broker, but they are not uniformly cheap: a chunk PUT
                // writes up to 2 MiB (decoded) to disk and the commit does a
                // sidecar write plus an atomic replace, so both get 30s. The
                // rest -- list, begin, abort, delete, notes -- are small,
                // bounded FS work; 10s is generous headroom and still bails on
                // a broker that has stopped answering. The recording BYTES
                // (/recording?id=) are fetched directly, deliberately with no
                // deadline at all -- see downloadRecording.
                const REC_TIMEOUT_MS = new Map([
                    ['/recording/chunk', 30000],
                    ['/recording/commit', 30000],
                ]);
                const REC_TIMEOUT_DEFAULT_MS = 10000;
                // These deadlines are now TOTAL (#200): ctx.http keeps its
                // timer armed across the body read, where hostFetch's was
                // disarmed the moment the status line landed. A broker that
                // answers a commit's headers and then stalls the JSON now
                // fails at 30s instead of hanging until the tab is closed —
                // the numbers are unchanged, what they cover grew.
                async function recApi(hostId, path, opts) {
                    const o = Object.assign({}, opts || {});
                    if (o.timeoutMs === undefined) {
                        // The query string ('/recording/notes?id=...') is not
                        // part of the route key.
                        const route = path.split('?')[0];
                        o.timeoutMs = REC_TIMEOUT_MS.has(route)
                            ? REC_TIMEOUT_MS.get(route) : REC_TIMEOUT_DEFAULT_MS;
                    }
                    // NO ctx.signal on any recorder route. The save path is
                    // reached FROM a teardown disposer and is supposed to
                    // outlive the activation (#198's rule is about the work's
                    // lifetime, not the callback that starts it): an
                    // already-aborted signal here would drop the segment on
                    // every disable-with-a-live-recording and raise the sticky
                    // "recording not saved" notice. One recApi serves every
                    // route, so it carries no signal at all.
                    const r = await ctx.http.fetch(hostId || selfHostId(),
                                                   path, o);
                    // ctx.http NEVER rejects: a dead broker, a deadline and a
                    // cancel all arrive as {status: 0, error}. `status` is
                    // present on every complete answer, so status 0 is the
                    // one branch that means "no answer at all".
                    if (r.status === 0) {
                        return { ok: false,
                                 error: r.error === 'host_not_found'
                                     ? HOST_GONE : String(r.error || 'failed') };
                    }
                    const j = r.json;
                    if (!j || typeof j !== 'object' || Array.isArray(j)) {
                        // A broker predating #140 has no /recording/* routes at
                        // all and answers the catch-all 404 with HTML. Reported
                        // as 'HTTP 404' that reads as a recording that went
                        // missing; say what it actually is. A KNOWN route
                        // 404s with a JSON body, so it keeps its own error.
                        if (r.status === 404) {
                            return { ok: false,
                                     error: 'no recorder on this broker' };
                        }
                        return { ok: false, error: 'HTTP ' + r.status };
                    }
                    // Response.ok's range, restated: ctx.http reports the
                    // status and takes no view on it.
                    if (j.ok === undefined) {
                        j.ok = (r.status >= 200 && r.status < 300);
                    }
                    return j;
                }
                function recPost(hostId, path, body) {
                    return recApi(hostId, path, { method: 'POST', json: body });
                }
                // recApi resolves every failure — HTTP and transport alike —
                // to {ok:false,error} now that ctx.http never rejects. This
                // stays as the guard for the one rejection left: ctx.http
                // THROWS synchronously on a call-site defect (a relative path,
                // an unserializable body), and inside an async function that
                // surfaces as a rejected promise. A defect must not become a
                // silent unhandled rejection in a repaint.
                async function recTry(promise) {
                    try { return await promise; }
                    catch (e) {
                        return { ok: false,
                                 error: String((e && e.message) || e) };
                    }
                }
                // The recording BYTES (/recording?id=), shared by the download
                // and the player — the two callers that used to hand-roll
                // hostFetch + r.ok + an error-body re-read each. Deliberately
                // NO deadline (timeoutMs: 0): this streams up to
                // MAX_RECORDING_BYTES (256 MiB) and any fixed timer is a guess
                // about the user's link speed. Under #200 that opt-out is now
                // genuinely unbounded — there is no headers-only fallback
                // deadline left underneath it.
                //
                // Resolves {ok:true, text} or {ok:false, error, auth}. `auth`
                // is the one failure the user can fix, and the CALLER decides
                // what to do about it: ctx.http never pops the prompt itself
                // (#161's bug class), so the two call sites still call
                // promptFileHostAuth — from a click and from a window load,
                // never from a poll.
                async function fetchRecordingBody(hostId, recId) {
                    const r = await ctx.http.fetch(
                        hostId || selfHostId(),
                        '/recording?id=' + encodeURIComponent(recId),
                        { timeoutMs: 0 });
                    if (r.status === 0) {
                        return { ok: false,
                                 error: r.error === 'host_not_found'
                                     ? HOST_GONE
                                     : String(r.error || 'failed') };
                    }
                    if (r.status < 200 || r.status >= 300) {
                        // The error BODY, not just the status: a 401 carries
                        // {error:'auth_required'}, and reporting it as
                        // 'HTTP 401' loses the one failure the user can fix.
                        // The route serves octet-stream on success, so a JSON
                        // error body is parsed by ctx.http and a non-JSON one
                        // (the catch-all 404's HTML) is not — hence both reads.
                        const err = (r.json && r.json.error)
                            || ('HTTP ' + r.status);
                        return { ok: false, error: err,
                                 auth: err === 'auth_required' };
                    }
                    // `text`, not a Blob: /recording answers decoded NDJSON
                    // (app.py's _recording_get gunzips server-side) and the
                    // player parses that string anyway. The download re-encodes
                    // it, which is byte-exact for the UTF-8 the writer emits.
                    return { ok: true, text: (r.text === undefined) ? '' : r.text };
                }

                // ---- download ----------------------------------------------
                // The download must NEVER be a plain <a href> to the broker:
                // an anchor with `download` makes the browser file the SOURCE
                // URL in its Downloads list, where it outlives the session,
                // stays on screen and is re-triggerable via Retry. Fetch the
                // bytes instead (hostFetch keeps the token in a header, so it
                // is not in the URL at all since #144) and hand the anchor a
                // blob: URL, which carries only the origin. Same shape as the
                // file-manager's download fallback.
                //
                // Always re-fetches, even from the player, which already holds
                // the recording in recData: parseRecording is LOSSY (blank and
                // unparseable lines dropped, `d` replaced by `bytes`, a
                // non-numeric `t` coerced to 0), so re-serializing it would not
                // reproduce the stored file. A download must be the archived
                // artifact, byte for byte.
                const dlBusy = new Set();   // recKey()s with a download in flight
                function dlReport(report, msg) {
                    // A download outlives its window, so the caller's status
                    // line may be detached by the time we fail. The notice host
                    // is always in the document.
                    if (report) {
                        try { report(msg); return; } catch (_) {}
                    }
                    showNotice(msg, { sticky: true });
                }
                async function downloadRecording(hostId, recId, report) {
                    const host = recHost(hostId);
                    if (!host) {
                        dlReport(report, 'download failed: ' + HOST_GONE);
                        return;
                    }
                    // Repeat clicks used to be free (the browser streamed each
                    // one to disk); now each would buffer the whole file, so a
                    // double-click on a big recording doubles the memory.
                    // Keyed per (broker, id): the same id on two brokers is two
                    // different recordings, and blocking the second would be a
                    // silent no-op.
                    const busyKey = recKey(host.id, recId);
                    if (dlBusy.has(busyKey)) return;
                    dlBusy.add(busyKey);
                    try {
                        const r = await fetchRecordingBody(host.id, recId);
                        if (!r.ok) {
                            // Before this, a stale token (the broker restarted
                            // and minted a new one) SAVED the 401 JSON body as
                            // <id>.blrec — a 36-byte "recording" with no error
                            // shown anywhere. Re-open the login prompt instead.
                            // A transport failure lands here too now, rather
                            // than in the catch: ctx.http never rejects.
                            if (r.auth) promptFileHostAuth(host);
                            dlReport(report, 'download failed: ' + r.error);
                            return;
                        }
                        // The Blob is built from the decoded text and re-typed
                        // application/octet-stream. It used to be r.blob(),
                        // which kept the bytes opaque; ctx.http reads a body as
                        // text, and for the UTF-8 NDJSON this route serves the
                        // round trip is byte-exact.
                        const url = URL.createObjectURL(
                            new Blob([r.text],
                                     { type: 'application/octet-stream' }));
                        const a = document.createElement('a');
                        a.href = url;
                        a.download = recId + '.blrec';
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        // Deliberately NOT gated on the window still being open:
                        // the browser owns the transfer from the click onward,
                        // so closing the player right after pressing the button
                        // still lands the file — exactly as it did before this
                        // change. Only the failure MESSAGE is routed away from a
                        // detached node (dlReport).
                        setTimeout(function () {
                            URL.revokeObjectURL(url);
                        }, 10000);
                    } catch (e) {
                        // A network failure no longer arrives here (ctx.http
                        // resolves it), but a call-site defect — ctx.http's
                        // synchronous throw — and the Blob/anchor work below
                        // still can. Unhandled, that would be a silent no-op —
                        // strictly worse than the failed entry the download
                        // manager used to show.
                        dlReport(report,
                                 'download failed: '
                                 + String((e && e.message) || e));
                    } finally {
                        dlBusy.delete(busyKey);
                    }
                }

                // ---- base64 <-> bytes --------------------------------------
                function bytesToB64(u8) {
                    let s = '';
                    for (let i = 0; i < u8.length; i += 0x8000) {
                        s += String.fromCharCode.apply(
                            null, u8.subarray(i, i + 0x8000));
                    }
                    return btoa(s);
                }
                function b64ToBytes(b64) {
                    const s = atob(b64);
                    const u8 = new Uint8Array(s.length);
                    for (let i = 0; i < s.length; i++) u8[i] = s.charCodeAt(i);
                    return u8;
                }

                function fmtClock(ms) {
                    const s = Math.max(0, Math.floor(ms / 1000));
                    const m = Math.floor(s / 60);
                    const sec = s % 60;
                    return m + ':' + (sec < 10 ? '0' + sec : sec);
                }
                function fmtSize(n) {
                    if (n >= 1024 * 1024) {
                        return (n / (1024 * 1024)).toFixed(1) + ' MB';
                    }
                    return Math.max(1, Math.round(n / 1024)) + ' KB';
                }

                // ---- capture ----------------------------------------------
                const encoder = new TextEncoder();
                const onBeforeUnload = function (e) {
                    // A live recording is in-memory only — warn before a reload
                    // discards it.
                    e.preventDefault();
                    e.returnValue = '';
                };
                // Which live state a reload would actually destroy. A MANUAL
                // recording is someone's deliberate capture — prompt. An
                // AUTO-record one is not: with auto-record on, an unconditional
                // guard means every single page reload prompts, which is the
                // fastest way to get the whole feature switched off. Auto
                // segments are re-armed on the next load, so the cost is the
                // in-flight segment (disclosed in help.md).
                // A save in flight is ALSO unfinished work — the pre-#151 guard
                // dropped at `active.size === 0`, i.e. the instant ⏺ stopped
                // capture, so a reload during "saving…" lost the upload with no
                // warning. Rolling makes that window far more common.
                function syncUnloadGuard() {
                    let manual = pendingSaves > 0;
                    if (!manual) {
                        for (const r of active.values()) {
                            if (!r.auto) { manual = true; break; }
                        }
                    }
                    // addEventListener/removeEventListener with the same
                    // (type, fn) pair are both idempotent, so no add/remove
                    // bookkeeping is needed.
                    if (manual) {
                        window.addEventListener('beforeunload', onBeforeUnload);
                    } else {
                        window.removeEventListener('beforeunload', onBeforeUnload);
                    }
                }

                // A segment-chain id. crypto.randomUUID() is deliberately NOT
                // used: it is secure-context only, and Browserland is routinely
                // served from a plain-http LAN origin where it is absent.
                function newSeriesId() {
                    let rand = '';
                    try {
                        const u = new Uint8Array(6);
                        crypto.getRandomValues(u);
                        for (const b of u) {
                            rand += (b + 0x100).toString(16).slice(1);
                        }
                    } catch (_) {
                        rand = Math.random().toString(36).slice(2, 14);
                    }
                    return 's' + Date.now().toString(36) + '-' + rand;
                }

                // opts (#151): {auto} marks a recording as auto-record-owned
                // (governs the unload guard and the setting's off switch), and
                // {series, seg} continue an existing chain — a roll passes the
                // previous segment's chain forward, a fresh start mints one.
                // Returns the live recording, or null when nothing started:
                // a roll MUST be able to tell that its replacement is really in
                // place, since it has already torn the previous one down.
                function startRecording(win, info, ui, opts) {
                    if (!win || win.disposed || !win.term || active.has(win.id)) {
                        return null;
                    }
                    const o = opts || {};
                    const t0 = performance.now();
                    const rec = {
                        win: win, ui: ui, info: info,
                        events: [], bytes: 0, t0: t0,
                        startedAt: Date.now(),
                        cols: win.term.cols, rows: win.term.rows,
                        title: (win.name || ('#' + info.wireId)),
                        fontFamily: (win.term.options
                                     && win.term.options.fontFamily)
                                    || TERM_FONT_BASELINE,
                        fontSize: (win.term.options
                                   && win.term.options.fontSize) || 14,
                        ws: win.ws,
                        rawWrite: win.term.write,
                        rawResize: win.term.resize,
                        wrapWrite: null, wrapResize: null,
                        inputDisp: null, ticker: null,
                        stopped: false,
                        auto: !!o.auto,
                        series: o.series || newSeriesId(),
                        seg: o.seg || 1,
                        rollTimer: null, rolling: false,
                    };
                    const now = function () {
                        return Math.max(0, Math.round(performance.now() - t0));
                    };
                    // Seed with the CURRENT screen so playback starts from
                    // what the terminal looked like at ⏺ (or at the segment
                    // boundary a roll opened), not a blank grid. Degrades to a
                    // blank start if the serialize addon didn't load (same
                    // fallback as the playback keyframes). dispose() is in a
                    // finally: a serialize() throw used to skip it, and with
                    // #151 rolling on a long session that leaks one addon per
                    // segment rather than one per ⏺ click.
                    try {
                        const SerCls = (typeof SerializeAddon !== 'undefined'
                                        && SerializeAddon
                                        && SerializeAddon.SerializeAddon)
                                       || null;
                        if (SerCls) {
                            const ser = new SerCls();
                            win.term.loadAddon(ser);
                            let snap = '';
                            try { snap = ser.serialize({ scrollback: 0 }); }
                            finally { try { ser.dispose(); } catch (_) {} }
                            if (snap) {
                                // encode ONCE: rec.bytes counts PTY bytes, and
                                // snap.length is a UTF-16 unit count, so a
                                // non-ASCII screen used to be under-counted.
                                const seed = encoder.encode(snap);
                                rec.events.push({ t: 0, k: 'o',
                                                  d: bytesToB64(seed) });
                                rec.bytes += seed.length;
                            }
                        }
                    } catch (_) {}
                    // The grid and the font a recording is FILED as (meta.cols /
                    // meta.rows is what a player window sizes itself to, and
                    // meta.fontFamily what it renders with). Re-read at the
                    // first captured byte as well as at start, because #151 arms
                    // capture from onTerminalCreate, where neither is settled
                    // yet: the termfont mod restyles the terminal from its own
                    // hook, and core fits the grid a round trip later.
                    //
                    // Prefer win.lastSentDims -- the grid core MEASURED and
                    // handed to the agent, i.e. the size the PTY laid these
                    // bytes out for -- over xterm's own grid, which lags it.
                    // MEASURED, honestly: the very first bytes of a session (a
                    // shell prompt, or the reattach redraw) reliably beat that
                    // measurement, so a just-opened terminal is still filed at
                    // the pristine 80x24 and carries an early resize event.
                    // That is faithful -- it is what the live terminal did, and
                    // the player follows recorded resizes -- so playback of an
                    // auto-started recording opens at 80x24 and resizes a
                    // moment in. Closing that cosmetic gap needs the player to
                    // adopt an opening resize, not a better guess here:
                    // FitAddon.proposeDimensions() disagrees with core's own
                    // arithmetic by a couple of columns, and filing a size the
                    // bytes were NOT laid out for would wrap the replay wrong.
                    const adoptGeom = function () {
                        const t = win.term;
                        if (!t) return;
                        const dims = win.lastSentDims;
                        rec.cols = (dims && dims.cols) || t.cols;
                        rec.rows = (dims && dims.rows) || t.rows;
                        rec.fontFamily = (t.options && t.options.fontFamily)
                            || rec.fontFamily;
                        rec.fontSize = (t.options && t.options.fontSize)
                            || rec.fontSize;
                    };
                    // A ⏺ on a terminal that is already up and running: adopt
                    // now, since its seed snapshot means the first-byte pass
                    // below is skipped.
                    adoptGeom();
                    const pushOut = function (u8) {
                        if (rec.stopped) return;
                        if (!rec.events.length) adoptGeom();
                        // A ws swap since the last chunk = a disconnect healed
                        // by a reattach snapshot — mark the gap on the timeline.
                        // A null -> socket transition is the FIRST attach, not a
                        // drop: auto-record arms the terminal before openWindow
                        // has dialled the broker, so an unconditional gap would
                        // stamp a red "connection lost" marker at 0:00 on every
                        // auto-started recording.
                        if (win.ws !== rec.ws) {
                            if (rec.ws) rec.events.push({ t: now(), k: 'g' });
                            rec.ws = win.ws;
                        }
                        // Encode BEFORE the counters move: bytesToB64 allocates
                        // and can throw under memory pressure, and the caller
                        // swallows that — a segment must not claim bytes whose
                        // event never made it into the array.
                        const d = bytesToB64(u8);
                        rec.bytes += u8.length;
                        rec.events.push({ t: now(), k: 'o', d: d });
                        maybeRoll(win, rec);
                    };
                    // Wrap THIS terminal's write. `this`, the optional callback
                    // and the return value all pass through untouched; restore
                    // only if the method is still our wrapper (another patcher
                    // may have stacked on top since).
                    rec.wrapWrite = function (data, cb) {
                        try {
                            if (typeof data === 'string') {
                                if (data) pushOut(encoder.encode(data));
                            } else if (data && data.length !== undefined) {
                                pushOut(data instanceof Uint8Array
                                        ? data : new Uint8Array(data));
                            }
                        } catch (_) {}
                        return rec.rawWrite.call(win.term, data, cb);
                    };
                    rec.wrapResize = function (cols, rows) {
                        // rec.stopped guard (also in pushOut): if ANOTHER
                        // patcher stacked on top of ours after start, stop
                        // can't unhook us — the wrapper must at least go
                        // inert instead of capturing into a dead recording.
                        try {
                            if (!rec.stopped) {
                                rec.events.push({ t: now(), k: 'r',
                                                  c: cols, r: rows });
                                // Every append path checks the cap, or the
                                // event-count ceiling does not actually bound
                                // the array — a session driven only by resizes
                                // would grow it with nothing to trip.
                                maybeRoll(win, rec);
                            }
                        } catch (_) {}
                        return rec.rawResize.call(win.term, cols, rows);
                    };
                    win.term.write = rec.wrapWrite;
                    win.term.resize = rec.wrapResize;
                    // Input MARKERS only: timestamp + byte count, never content
                    // (typed passwords are unechoed — content would leak them).
                    rec.inputDisp = win.term.onData(function (d) {
                        if (rec.stopped) return;
                        rec.events.push({ t: now(), k: 'i',
                                          n: (d && d.length) || 0 });
                        // Also checked here, not only on the output path: input
                        // markers cost an array slot and no `bytes`, so a
                        // session that types a lot and prints nothing would
                        // never reach a cap check at all.
                        maybeRoll(win, rec);
                    });
                    rec.ticker = setInterval(function () {
                        ui.setLabel(fmtClock(now()));
                    }, 1000);
                    active.set(win.id, rec);
                    syncUnloadGuard();
                    ui.setRecording(true);
                    ui.setLabel('0:00');
                    return rec;
                }

                // ---- segment roll (#151) -----------------------------------
                // The cap used to stopRecording(win, 'size cap'): capture just
                // ENDED, silently, and everything after was lost. Tolerable for
                // a deliberate manual recording, useless for an always-on one.
                //
                // The roll is DEFERRED by a task rather than run inline from the
                // write wrapper. Nothing is lost by waiting: the old segment is
                // still patched and still capturing until the timer fires, so
                // the only cost is a bounded overshoot (one task's worth of
                // frames past the cap). What it buys is that the un-patch ->
                // re-patch never happens re-entrantly inside term.write, and
                // the new segment's seed snapshot is serialized from a clean
                // task, where xterm has had a chance to drain writes it had
                // only queued.
                function maybeRoll(win, rec) {
                    if (rec.stopped || rec.rolling) return;
                    if (rec.bytes <= REC_CAP_BYTES
                        && rec.events.length <= REC_CAP_EVENTS) return;
                    rec.rolling = true;
                    rec.rollTimer = setTimeout(function () {
                        rec.rollTimer = null;
                        // Anything that stopped or replaced this recording in
                        // the meantime (⏺, window close, auto-record switched
                        // off) wins — never roll a dead segment.
                        if (rec.stopped || active.get(win.id) !== rec) return;
                        // Wait for xterm's write queue to DRAIN before rolling.
                        // A task boundary alone is not enough: term.write only
                        // parses within a time budget and finishes the rest on
                        // its own timer, so the next segment's seed snapshot
                        // could be serialized from a screen that is missing the
                        // very output that tripped the cap — and if that output
                        // cleared the screen or moved the cursor, part N+1 would
                        // replay from the wrong state. term.write('', cb) is the
                        // same completion barrier the keyframe pass uses
                        // (flushTerm). Overshoot is free: this segment is still
                        // patched and still capturing until the roll lands.
                        win.term.write('', function () {
                            if (rec.stopped || active.get(win.id) !== rec) return;
                            rollRecording(win, rec);
                        });
                    }, 0);
                }
                function rollRecording(win, rec) {
                    const info = rec.info, ui = rec.ui;
                    const chain = { auto: rec.auto, series: rec.series,
                                    seg: rec.seg + 1 };
                    // No await between the un-patch and the re-patch (the upload
                    // is fired off, not awaited), and JS is single-threaded, so
                    // no PTY byte can arrive while term.write is unwrapped.
                    stopRecording(win, 'size cap');
                    const next = startRecording(win, info, ui, chain);
                    if (!next) {
                        // The previous segment is already torn down and saving,
                        // so a failed replacement means capture has ENDED —
                        // exactly the silence this change exists to remove. Say
                        // so instead of leaving a dark ⏺ to be noticed later.
                        showNotice('recording stopped at the size cap — '
                                   + 'could not start the next part',
                                   { sticky: true });
                    }
                    return next;
                }

                function stopRecording(win, reason) {
                    const rec = active.get(win.id);
                    if (!rec || rec.stopped) return;
                    // win.id is '<hostId>:<pid>', which a later terminal on the
                    // same broker can reuse, and a lost-lease view rebuild
                    // disposes every window and re-opens a fresh one under the
                    // same key. A stale teardown must not stop the recording of
                    // the window that took over the id.
                    if (rec.win !== win) return;
                    rec.stopped = true;
                    active.delete(win.id);
                    syncUnloadGuard();
                    if (rec.rollTimer !== null) clearTimeout(rec.rollTimer);
                    if (rec.ticker) clearInterval(rec.ticker);
                    // Un-patch only if still ours (see wrap note above).
                    try {
                        if (win.term && win.term.write === rec.wrapWrite) {
                            win.term.write = rec.rawWrite;
                        }
                        if (win.term && win.term.resize === rec.wrapResize) {
                            win.term.resize = rec.rawResize;
                        }
                    } catch (_) {}
                    try { if (rec.inputDisp) rec.inputDisp.dispose(); }
                    catch (_) {}
                    rec.durationMs = Math.max(
                        0, Math.round(performance.now() - rec.t0));
                    rec.ui.setRecording(false);
                    if (!rec.events.length) {
                        rec.ui.setLabel('');
                        return;
                    }
                    rec.ui.setLabel('saving…');
                    // The title-bar label belongs to whatever is recording on
                    // that terminal NOW. On a roll the next segment is already
                    // running by the time this resolves, so writing 'saved ✓' /
                    // '' into it would stomp the live clock. A FAILURE is never
                    // swallowed that way — it goes to a sticky notice, so a
                    // segment that did not land is visible whether or not the
                    // terminal has moved on (this is also how a full disk, or
                    // MAX_RECORDING_SESSIONS backpressure, surfaces).
                    pendingSaves++;
                    syncUnloadGuard();
                    // Which save owns this title bar's label. Several segments
                    // of one terminal can be in flight at once (rolling, or the
                    // off switch), and they do not finish in order — without a
                    // generation a slow part 1 landing late would overwrite
                    // part 2's 'save failed' with 'saved ✓', and its 4s clear
                    // would wipe the newer result.
                    const ui = rec.ui;
                    ui.saveGen = (ui.saveGen || 0) + 1;
                    const myGen = ui.saveGen;
                    enqueueSave(rec).then(function (res) {
                        pendingSaves--;
                        syncUnloadGuard();
                        const partOf = rec.seg > 1
                            ? (' (part ' + rec.seg + ')') : '';
                        if (!res.ok) {
                            showNotice('recording not saved' + partOf + ': '
                                       + (res.error || 'unknown error'),
                                       { sticky: true });
                        }
                        if (ui.saveGen === myGen && !active.has(win.id)) {
                            ui.setLabel(res.ok ? 'saved ✓' : 'save failed');
                            setTimeout(function () {
                                if (ui.saveGen === myGen
                                    && !active.has(win.id)) ui.setLabel('');
                            }, 4000);
                        }
                        if (libRender) libRender();
                    });
                }

                // Uploads are QUEUED, not fired the moment capture stops. The
                // broker allows MAX_RECORDING_SESSIONS (4) concurrent save
                // sessions and 429s `too_many_sessions` beyond that, which loses
                // the segment outright — and #151 makes many-at-once ordinary:
                // the off switch stops every auto recording in one go, and a
                // rolling terminal hands over a segment while the next one is
                // already filling. Queuing also caps peak memory, since each
                // concurrent save transiently holds its segment twice (the
                // event array plus the serialized payload).
                const SAVE_SLOTS = 2;   // < 4, so a manual ⏺ save is never starved
                const saveQueue = [];
                let saveRunning = 0;
                function enqueueSave(rec) {
                    return new Promise(function (resolve) {
                        saveQueue.push({ rec: rec, resolve: resolve });
                        pumpSaves();
                    });
                }
                function pumpSaves() {
                    while (saveRunning < SAVE_SLOTS && saveQueue.length) {
                        const job = saveQueue.shift();
                        saveRunning++;
                        // .catch BEFORE .then, not after: saveRecording
                        // serializes the whole segment outside its own HTTP
                        // try/catch, so an allocation failure at exactly the
                        // sizes rolling produces would REJECT. Unhandled, that
                        // strands the caller's pendingSaves counter — leaving
                        // the reload guard armed forever and the title-bar label
                        // stuck on "saving…".
                        saveRecording(job.rec).catch(function (e) {
                            return { ok: false,
                                     error: String((e && e.message) || e) };
                        }).then(function (res) {
                            saveRunning--;
                            job.resolve(res);
                            pumpSaves();
                        });
                    }
                }

                async function saveRecording(rec) {
                    // Yield a task BEFORE serializing. Everything down to the
                    // first network await is synchronous work over the whole
                    // event array — up to a cap's worth of base64 — and #151
                    // reaches this from the roll, i.e. from a timer that fired
                    // off the terminal's output path. Deferring keeps that
                    // stringify out of the same task as the roll itself.
                    await new Promise(function (r) { setTimeout(r, 0); });
                    const meta = {
                        v: 1, title: rec.title,
                        cols: rec.cols, rows: rec.rows,
                        startedAt: rec.startedAt,
                        durationMs: rec.durationMs,
                        fontFamily: rec.fontFamily, fontSize: rec.fontSize,
                        events: rec.events.length, bytes: rec.bytes,
                        // #151 chain: which rolling recording this segment
                        // belongs to, and where in it.
                        series: rec.series, seg: rec.seg,
                    };
                    const lines = [JSON.stringify(meta)];
                    for (const ev of rec.events) lines.push(JSON.stringify(ev));
                    const payload = encoder.encode(lines.join('\n') + '\n');
                    // Drop the event array now the payload exists: the upload
                    // holds the whole segment in memory for the duration of the
                    // begin/chunk/commit round trip, and with rolling there can
                    // be a previous segment still in flight while the next one
                    // fills. Nothing reads rec.events after this point.
                    rec.events = [];
                    let recId = null;
                    // Once the commit is in flight the server may already have
                    // replaced the .blrec, so the cleanup below must stop: an
                    // abort racing a slow commit pops the session and unlinks
                    // the temp out from under it, destroying a recording that
                    // was about to land. Cleanup is for a failure BEFORE commit.
                    let committing = false;
                    // Pinned to the ORIGIN broker (#161): capture happens in
                    // this browser, so this is the one recorder path that does
                    // not take a host — the recording is filed here even when
                    // the terminal it came from runs on a remote broker. Named
                    // ONCE, and named EXPLICITLY (#200): the whole three-call
                    // begin/chunk/commit sequence must land on one broker, so
                    // re-resolving per call would let a registry edit mid-save
                    // send the commit somewhere the session does not exist.
                    const selfId = selfHostId();
                    try {
                        const b = await recPost(selfId, '/recording/begin', {});
                        if (!b.ok) return b;
                        recId = b.recording_id;
                        for (let off = 0; off < payload.length; off += CHUNK_RAW) {
                            const part = payload.subarray(off, off + CHUNK_RAW);
                            const c = await recPost(selfId, '/recording/chunk', {
                                recording_id: recId, offset: off,
                                content_b64: bytesToB64(part),
                            });
                            if (!c.ok) throw new Error(c.error || 'chunk');
                        }
                        committing = true;
                        return await recPost(selfId, '/recording/commit',
                                             { recording_id: recId, meta: meta });
                    } catch (e) {
                        // Best-effort cleanup; the ORIGINAL failure is what
                        // gets reported (mirrors the upload_abort contract).
                        if (recId && !committing) {
                            // The sync try/catch never saw this promise's
                            // REJECTION, so a failed cleanup became an
                            // unhandled rejection in the console. recApi no
                            // longer rejects on a transport failure (#200), but
                            // ctx.http's synchronous argument throw still lands
                            // as one, and cleanup stays fire-and-forget so the
                            // ORIGINAL failure is what gets reported.
                            recPost(selfId, '/recording/abort',
                                    { recording_id: recId }).catch(() => {});
                        }
                        return { ok: false, error: String(e && e.message || e) };
                    }
                }

                // ---- per-terminal ⏺ button --------------------------------
                const decorated = new WeakSet();
                ctx.windows.onTerminalCreate(function (info) {
                    const win = info.win;
                    if (!win || !info.titleBar || decorated.has(win)) return;
                    decorated.add(win);

                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'tb-btn btn-rec';
                    btn.title = 'Record session';
                    btn.textContent = '⏺';
                    const label = document.createElement('span');
                    label.className = 'rec-label';
                    info.addTitleBarItem(btn);
                    info.addTitleBarItem(label);

                    const ui = {
                        setRecording: function (on) {
                            btn.classList.toggle('recording', !!on);
                            btn.title = on ? 'Stop recording (saves)'
                                           : 'Record session';
                        },
                        setLabel: function (s) { label.textContent = s; },
                    };
                    const onClick = function (e) {
                        e.stopPropagation();
                        if (active.has(win.id)) {
                            // A ⏺ stop must STAY stopped: without this, the
                            // next auto-record pass (a setting flip, or this
                            // window being rebuilt) re-arms the very terminal
                            // the button was just used to switch off.
                            userStopped.add(win.id);
                            stopRecording(win, 'user');
                        } else {
                            userStopped.delete(win.id);
                            // While auto-record is on, a ⏺ start re-arms the
                            // AUTO recording rather than opening a manual one —
                            // otherwise one terminal would sit in manual mode
                            // (reload prompt, immune to the off switch) with
                            // nothing on screen saying so.
                            startRecording(win, info, ui,
                                           { auto: autoSetting.get() });
                        }
                    };
                    const stopProp = function (e) { e.stopPropagation(); };
                    btn.addEventListener('mousedown', stopProp);
                    btn.addEventListener('click', onClick);

                    let torn = false;
                    const teardown = function () {
                        if (torn) return;
                        torn = true;
                        disposers.delete(teardown);
                        // A window closing (or the mod unloading) mid-recording
                        // stops + saves what was captured.
                        if (active.has(win.id)) stopRecording(win, 'teardown');
                        // Only if this window still owns the id — a lost-lease
                        // rebuild re-opens under the same key, so a late
                        // teardown must not evict the live replacement, nor
                        // erase a ⏺ stop that was made on it. (Core returns the
                        // existing window for a known id, so a replacement can
                        // only appear after this window is gone; the guard is
                        // cheap insurance either way.)
                        const reg = terms.get(win.id);
                        if (!reg || reg.win === win) {
                            terms.delete(win.id);
                            userStopped.delete(win.id);
                        }
                        btn.removeEventListener('mousedown', stopProp);
                        btn.removeEventListener('click', onClick);
                        btn.remove();
                        label.remove();
                    };
                    disposers.add(teardown);
                    info.onDispose(teardown);

                    terms.set(win.id, { win: win, info: info, ui: ui });
                    // Arm auto-record for a terminal opening now — immediately,
                    // not on a later frame, so no output can arrive before
                    // capture is wrapped around term.write. The grid is still
                    // the pre-fit default at this point; adoptGeom (above)
                    // re-reads it at the first captured byte so the recording is
                    // filed at the size it was actually played out on.
                    if (autoSetting.get() && !userStopped.has(win.id)) {
                        startRecording(win, info, ui, { auto: true });
                    }
                });

                // Flipping the setting ON arms every terminal already open (the
                // create hook has long since fired for them) and clears any ⏺
                // stops — a fresh opt-in should mean "record everything", not
                // "record everything except the ones I once switched off".
                //
                // Flipping it OFF stops (and saves) the auto recordings it
                // started. #151 floated the opposite — off governs only what
                // STARTS — and it is the wrong default: with rolling, an auto
                // recording left running after the switch is off never ends by
                // itself, so the toggle would read as on-only. Manual ⏺
                // recordings are untouched either way; they are not the
                // setting's to stop.
                autoSetting.onChange(function (on) {
                    if (on) {
                        userStopped.clear();
                        for (const t of Array.from(terms.values())) {
                            if (t.win.disposed || active.has(t.win.id)) continue;
                            startRecording(t.win, t.info, t.ui, { auto: true });
                        }
                        return;
                    }
                    for (const rec of Array.from(active.values())) {
                        if (rec.auto) stopRecording(rec.win, 'auto-record off');
                    }
                });

                // ---- recording load + keyframe index ----------------------
                function parseRecording(text) {
                    const lines = text.split('\n');
                    let meta = null;
                    const events = [];
                    for (const line of lines) {
                        if (!line) continue;
                        let obj;
                        try { obj = JSON.parse(line); } catch (_) { continue; }
                        if (!meta) { meta = obj; continue; }
                        if (obj.k === 'o' && typeof obj.d === 'string') {
                            obj.bytes = b64ToBytes(obj.d);
                            delete obj.d;
                        }
                        if (typeof obj.t !== 'number') obj.t = 0;
                        events.push(obj);
                    }
                    if (!meta) throw new Error('empty recording');
                    return { meta: meta, events: events };
                }
                function flushTerm(term) {
                    return new Promise(function (res) { term.write('', res); });
                }
                // Offscreen pass building the keyframe index: replay everything
                // once, snapshotting (after an awaited flush, so the frame is
                // exact at its event boundary) every KF_MS / KF_BYTES. kf.idx
                // is the LAST event index applied. kf[0] is the blank start.
                async function buildKeyframes(recData, onProgress) {
                    const meta = recData.meta, events = recData.events;
                    const kfs = [{ idx: -1, t: 0, data: '',
                                   c: meta.cols || 80, r: meta.rows || 24 }];
                    const SerCls = (typeof SerializeAddon !== 'undefined'
                                    && SerializeAddon
                                    && SerializeAddon.SerializeAddon) || null;
                    if (!SerCls) return kfs;   // degrade: replay-from-start
                    const host = document.createElement('div');
                    host.style.cssText =
                        'position:fixed;left:-10000px;top:0;';
                    document.body.appendChild(host);
                    const term = new Terminal({
                        cols: meta.cols || 80, rows: meta.rows || 24,
                        scrollback: 200, cursorBlink: false,
                        fontFamily: meta.fontFamily || TERM_FONT_BASELINE,
                        fontSize: meta.fontSize || 14,
                        theme: { background: '#000000' },
                    });
                    const ser = new SerCls();
                    term.loadAddon(ser);
                    term.open(host);
                    try {
                        let since = 0, lastKfT = 0;
                        for (let i = 0; i < events.length; i++) {
                            const ev = events[i];
                            if (ev.k === 'o' && ev.bytes) {
                                term.write(ev.bytes);
                                since += ev.bytes.length;
                            } else if (ev.k === 'r') {
                                await flushTerm(term);
                                term.resize(ev.c || term.cols,
                                            ev.r || term.rows);
                            }
                            if (since >= KF_BYTES
                                || (since > 0 && ev.t - lastKfT >= KF_MS)) {
                                await flushTerm(term);
                                let data = '';
                                try { data = ser.serialize({ scrollback: 0 }); }
                                catch (_) {}
                                kfs.push({ idx: i, t: ev.t, data: data,
                                           c: term.cols, r: term.rows });
                                since = 0;
                                lastKfT = ev.t;
                                if (onProgress) {
                                    onProgress(i + 1, events.length);
                                }
                            }
                        }
                        await flushTerm(term);
                    } finally {
                        try { term.dispose(); } catch (_) {}
                        host.remove();
                    }
                    return kfs;
                }

                // ---- player window (EPHEMERAL, fixed at recorded size) -----
                function openPlayerWindow(appData) {
                    const recId = String(appData.recId || '');
                    const id = String(appData.id);
                    const meta0 = appData.meta || {};
                    // #161: which broker STORES this recording. Kept as an id,
                    // not a host object (see recHost), and deliberately NOT
                    // written to win.hostId — core reads that as the broker a
                    // TERMINAL belongs to and drives visibility masking, auth
                    // healing and reattachment off it. App windows stay 'app'.
                    const recHostId = String(appData.hostId || 'local');
                    // The stored title is the plain session name for every
                    // segment; the part number is DERIVED from meta.seg here
                    // rather than baked into the title, so `seg` stays the one
                    // source of truth for a chain's ordering. A non-local
                    // broker is named too — otherwise two same-named sessions
                    // from two brokers give identical windows and taskbar
                    // labels.
                    const title = 'Playback — '
                        + (meta0.title || recId)
                        + (meta0.seg > 1 ? (' (part ' + meta0.seg + ')') : '')
                        + (recHostId !== 'local'
                           ? (' · ' + recHostLabel(recHost(recHostId))) : '');
                    const geom = clampGeom(appData.geom
                                           || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color
                                               || defaultColor(id));
                    const chrome = buildAppChrome({
                        id: id, appClass: 'app-recplay', badge: '#rec',
                        geom: geom, color: color, locked: true, title: title,
                    });
                    const dom = chrome.dom;

                    const termHost = document.createElement('div');
                    termHost.className = 'recplay-term';
                    const bar = document.createElement('div');
                    bar.className = 'app-toolbar recplay-bar';
                    const notesEl = document.createElement('div');
                    notesEl.className = 'recplay-notes';
                    dom.appendChild(termHost);
                    dom.appendChild(bar);
                    dom.appendChild(notesEl);
                    // NO resize handles: the recording dictates the window
                    // size (the whole point of same-size playback).
                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id: id, sid: 'rec', hostId: 'app',
                        type: 'app', appKind: 'recplayer',
                        dom: dom, body: termHost, titleText: chrome.titleText,
                        term: null, fitAddon: null,
                        ws: null, wsOpen: false, termReady: false,
                        minimized: false, disposed: false,
                        geom: geom, name: title, color: color,
                        resizeTimer: null, lastSentDims: null,
                        staleSession: false, authFailed: false,
                        reattachAttempts: 0, reattachAt: 0,
                        lastOpenAt: 0, missingPolls: 0,
                        cleanups: [], tiled: false, floatGeom: null,
                        locked: true, dirty: false,
                    };
                    windows.set(id, win);
                    wireAppChrome(win, chrome);

                    const appSess = { key: id, sid: 'rec', id: id,
                                      title: title, stale: false,
                                      kind: 'app', hostId: 'app' };
                    sessions.set(id, appSess);
                    const itemsHost = document.getElementById('taskbar-items');
                    if (!itemsHost.querySelector(
                            '.taskbar-item[data-session-id="'
                            + cssEscape(id) + '"]')) {
                        itemsHost.appendChild(buildTaskbarItem(appSess));
                    }
                    updateTaskbarColor(id);
                    updateTaskbarLabel(id);
                    const emptyMsg = document.getElementById('taskbar-empty');
                    if (emptyMsg) emptyMsg.remove();

                    // ---- transport bar UI ---------------------------------
                    const mkBtn = function (txt, tip) {
                        const b = document.createElement('button');
                        b.type = 'button';
                        b.className = 'recplay-btn';
                        b.textContent = txt;
                        b.title = tip;
                        b.addEventListener('mousedown',
                                           function (e) { e.stopPropagation(); });
                        return b;
                    };
                    const playBtn = mkBtn('▶', 'play / pause');
                    const revBtn = mkBtn('◀◀', 'play in reverse');
                    const speedSel = document.createElement('select');
                    speedSel.className = 'recplay-speed';
                    for (const s of SPEEDS) {
                        const o = document.createElement('option');
                        o.value = String(s);
                        o.textContent = s + '×';
                        if (s === 1) o.selected = true;
                        speedSel.appendChild(o);
                    }
                    const scrubWrap = document.createElement('div');
                    scrubWrap.className = 'recplay-scrubwrap';
                    const scrub = document.createElement('input');
                    scrub.type = 'range';
                    scrub.className = 'recplay-scrub';
                    scrub.min = '0'; scrub.max = '1000'; scrub.value = '0';
                    const markers = document.createElement('div');
                    markers.className = 'recplay-markers';
                    scrubWrap.appendChild(scrub);
                    scrubWrap.appendChild(markers);
                    const timeEl = document.createElement('span');
                    timeEl.className = 'recplay-time';
                    timeEl.textContent = '0:00 / 0:00';
                    const noteBtn = mkBtn('✎+', 'add a note at the current time');
                    const dlBtn = mkBtn('⬇', 'download recording file');
                    const statusEl = document.createElement('span');
                    statusEl.className = 'recplay-status';
                    bar.appendChild(playBtn);
                    bar.appendChild(revBtn);
                    bar.appendChild(speedSel);
                    bar.appendChild(scrubWrap);
                    bar.appendChild(timeEl);
                    bar.appendChild(noteBtn);
                    bar.appendChild(dlBtn);
                    bar.appendChild(statusEl);

                    // ---- player state -------------------------------------
                    let recData = null, kfs = null, duration = 1;
                    let pos = 0, playing = false, dirRev = false, speed = 1;
                    let evIdx = 0, gen = 0, rendering = false;
                    let pendingSeek = null, raf = null, lastTs = null;
                    let lastRevRender = 0, lastUiSync = 0;
                    let notes = [], notesRev = 0;
                    let closed = false;
                    let term = null;

                    const setStatus = function (s) {
                        statusEl.textContent = s || '';
                    };

                    function cellDims() {
                        try {
                            const dims = term._core._renderService.dimensions;
                            const w = dims.actualCellWidth
                                || (dims.css && dims.css.cell
                                    && dims.css.cell.width);
                            const h = dims.actualCellHeight
                                || (dims.css && dims.css.cell
                                    && dims.css.cell.height);
                            if (w && h) return { w: w, h: h };
                        } catch (_) {}
                        return { w: 9, h: 17 };
                    }
                    // Fix the window box to the terminal grid (+chrome): the
                    // recording's size dictates the window's, incl. recorded
                    // mid-session resizes.
                    function sizeToGrid() {
                        const d = cellDims();
                        const tw = Math.ceil(term.cols * d.w) + 12;
                        const th = Math.ceil(term.rows * d.h) + 12;
                        termHost.style.width = tw + 'px';
                        termHost.style.height = th + 'px';
                        const w = Math.max(tw + 4, 560);
                        const h = chrome.titleBar.offsetHeight + th
                            + bar.offsetHeight + notesEl.offsetHeight + 12;
                        dom.style.width = w + 'px';
                        dom.style.height = h + 'px';
                        win.geom = { left: win.geom.left, top: win.geom.top,
                                     width: w + 4, height: h + 4 };
                    }

                    function apply(ev) {
                        // Everything rides xterm's single write queue so
                        // output and resizes land IN ORDER: a bare resize
                        // would apply immediately, ahead of still-queued
                        // output, corrupting TUIs — so resizes are queued as
                        // write callbacks instead.
                        if (ev.k === 'o' && ev.bytes) term.write(ev.bytes);
                        else if (ev.k === 'r') {
                            term.write('', function () {
                                term.resize(ev.c || term.cols,
                                            ev.r || term.rows);
                                sizeToGrid();
                            });
                        }
                    }
                    function eventIndexAt(t) {
                        // last event with .t <= t (binary search)
                        const evs = recData.events;
                        let lo = 0, hi = evs.length - 1, ans = -1;
                        while (lo <= hi) {
                            const mid = (lo + hi) >> 1;
                            if (evs[mid].t <= t) { ans = mid; lo = mid + 1; }
                            else hi = mid - 1;
                        }
                        return ans;
                    }
                    // Re-render the screen as of time `target` from the nearest
                    // keyframe at or before it. Generation-tokened: a newer
                    // seek always wins, a stale async completion never paints.
                    function renderAt(target) {
                        if (closed || !recData) return;
                        // Bump gen on every REQUEST (not just render start):
                        // an in-flight render's completion must never commit
                        // evIdx once a newer target exists.
                        gen++;
                        if (rendering) { pendingSeek = target; return; }
                        rendering = true;
                        const myGen = gen;
                        const targetIdx = eventIndexAt(target);
                        let kf = kfs[0];
                        for (let i = kfs.length - 1; i >= 0; i--) {
                            if (kfs[i].idx <= targetIdx) { kf = kfs[i]; break; }
                        }
                        // Drain any still-queued playback writes BEFORE the
                        // reset (both queued as write callbacks), so stale
                        // output can never flush into the freshly-rendered
                        // frame; the keyframe + delta writes queue after.
                        term.write('', function () {
                            term.reset();
                            if (term.cols !== kf.c || term.rows !== kf.r) {
                                term.resize(kf.c, kf.r);
                                sizeToGrid();
                            }
                        });
                        if (kf.data) term.write(kf.data);
                        for (let i = kf.idx + 1; i <= targetIdx; i++) {
                            apply(recData.events[i]);
                        }
                        term.write('', function () {
                            rendering = false;
                            if (myGen === gen) evIdx = targetIdx + 1;
                            if (pendingSeek !== null) {
                                const p = pendingSeek;
                                pendingSeek = null;
                                renderAt(p);
                            }
                        });
                    }

                    function syncUi(force) {
                        const t = performance.now();
                        if (!force && t - lastUiSync < 100) return;
                        lastUiSync = t;
                        scrub.value = String(Math.round(
                            (pos / duration) * 1000));
                        timeEl.textContent =
                            fmtClock(pos) + ' / ' + fmtClock(duration);
                    }
                    function setPlaying(on) {
                        playing = on;
                        playBtn.textContent = on ? '⏸' : '▶';
                        if (on && raf === null) {
                            lastTs = null;
                            raf = requestAnimationFrame(tick);
                        }
                    }
                    function tick(ts) {
                        raf = null;
                        if (closed) return;
                        if (!playing) return;
                        if (lastTs === null) lastTs = ts;
                        const dt = (ts - lastTs) * speed;
                        lastTs = ts;
                        if (dirRev) {
                            pos = Math.max(0, pos - dt);
                            // Reverse renders whole frames from keyframes —
                            // throttled, frame-dropping (renderAt coalesces).
                            if (ts - lastRevRender > 80) {
                                lastRevRender = ts;
                                renderAt(pos);
                            }
                            if (pos <= 0) { renderAt(0); setPlaying(false); }
                        } else {
                            pos = Math.min(duration, pos + dt);
                            // A seek render owns the write queue + evIdx —
                            // stepping from a stale evIdx mid-render would
                            // interleave; the clock advances, events wait.
                            if (!rendering) {
                                const evs = recData.events;
                                while (evIdx < evs.length
                                       && evs[evIdx].t <= pos) {
                                    apply(evs[evIdx]);
                                    evIdx++;
                                }
                            }
                            if (pos >= duration) setPlaying(false);
                        }
                        syncUi(false);
                        if (playing) raf = requestAnimationFrame(tick);
                    }
                    function seekTo(t, keepPlaying) {
                        pos = Math.max(0, Math.min(duration, t));
                        renderAt(pos);
                        syncUi(true);
                        if (!keepPlaying) setPlaying(false);
                    }

                    playBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (!recData) return;
                        if (!playing && !dirRev && pos >= duration) {
                            seekTo(0, false);   // replay from the top
                        }
                        setPlaying(!playing);
                    });
                    revBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (!recData) return;
                        dirRev = !dirRev;
                        revBtn.classList.toggle('active', dirRev);
                        if (dirRev && !playing) setPlaying(true);
                    });
                    speedSel.addEventListener('change', function () {
                        speed = parseFloat(speedSel.value) || 1;
                    });
                    speedSel.addEventListener('mousedown',
                                              function (e) { e.stopPropagation(); });
                    scrub.addEventListener('input', function () {
                        if (!recData) return;
                        const t = (parseInt(scrub.value, 10) / 1000) * duration;
                        pos = t;
                        renderAt(t);
                        timeEl.textContent =
                            fmtClock(pos) + ' / ' + fmtClock(duration);
                    });
                    scrub.addEventListener('mousedown',
                                           function (e) { e.stopPropagation(); });
                    dlBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        downloadRecording(recHostId, recId, function (msg) {
                            // The player's own status line while it is still
                            // up; a notice once this window has gone.
                            if (closed) showNotice(msg, { sticky: true });
                            else setStatus(msg);
                        });
                    });

                    // ---- notes --------------------------------------------
                    function renderMarkers() {
                        markers.innerHTML = '';
                        if (!recData) return;
                        for (const ev of recData.events) {
                            if (ev.k !== 'g') continue;
                            const m = document.createElement('div');
                            m.className = 'recplay-mark recplay-mark-gap';
                            m.style.left =
                                ((ev.t / duration) * 100) + '%';
                            m.title = 'connection gap @ ' + fmtClock(ev.t);
                            markers.appendChild(m);
                        }
                        notes.forEach(function (n, i) {
                            const m = document.createElement('div');
                            m.className = 'recplay-mark recplay-mark-note';
                            m.style.left =
                                ((n.t / duration) * 100) + '%';
                            m.title = fmtClock(n.t) + ' — ' + n.text;
                            m.addEventListener('mousedown',
                                               function (e) { e.stopPropagation(); });
                            m.addEventListener('click', function (e) {
                                e.stopPropagation();
                                seekTo(n.t, false);
                                const row = notesEl.querySelector(
                                    '[data-note="' + i + '"]');
                                if (row) row.classList.add('flash');
                            });
                            markers.appendChild(m);
                        });
                    }
                    function renderNotes() {
                        notesEl.innerHTML = '';
                        notes.forEach(function (n, i) {
                            const row = document.createElement('div');
                            row.className = 'recplay-note';
                            row.dataset.note = String(i);
                            const tEl = document.createElement('span');
                            tEl.className = 'recplay-note-t';
                            tEl.textContent = fmtClock(n.t);
                            tEl.title = 'jump to ' + fmtClock(n.t);
                            tEl.addEventListener('click', function () {
                                seekTo(n.t, false);
                            });
                            const txt = document.createElement('span');
                            txt.className = 'recplay-note-text';
                            txt.textContent = n.text;
                            const edit = document.createElement('button');
                            edit.type = 'button';
                            edit.className = 'recplay-note-btn';
                            edit.textContent = '✎';
                            edit.title = 'edit note';
                            edit.addEventListener('click', function () {
                                openNoteEditor(n.t, n.text, function (text) {
                                    if (!text) return;
                                    notes[i] = { t: n.t, text: text };
                                    saveNotes();
                                });
                            });
                            const del = document.createElement('button');
                            del.type = 'button';
                            del.className = 'recplay-note-btn';
                            del.textContent = '✕';
                            del.title = 'delete note';
                            del.addEventListener('click', function () {
                                notes.splice(i, 1);
                                saveNotes();
                            });
                            row.appendChild(tEl);
                            row.appendChild(txt);
                            row.appendChild(edit);
                            row.appendChild(del);
                            notesEl.appendChild(row);
                        });
                        renderMarkers();
                        sizeToGrid();
                    }
                    let editorRow = null;
                    function openNoteEditor(t, initial, done) {
                        if (editorRow) editorRow.remove();
                        const row = document.createElement('div');
                        row.className = 'recplay-note recplay-note-editor';
                        const tEl = document.createElement('span');
                        tEl.className = 'recplay-note-t';
                        tEl.textContent = fmtClock(t);
                        const input = document.createElement('input');
                        input.type = 'text';
                        input.className = 'recplay-note-input';
                        input.placeholder = 'note @ ' + fmtClock(t)
                            + ' — Enter saves, Esc cancels';
                        input.value = initial || '';
                        input.addEventListener('keydown', function (e) {
                            e.stopPropagation();
                            if (e.key === 'Enter') {
                                const v = input.value.trim();
                                row.remove();
                                editorRow = null;
                                done(v);
                            } else if (e.key === 'Escape') {
                                row.remove();
                                editorRow = null;
                            }
                        });
                        row.appendChild(tEl);
                        row.appendChild(input);
                        notesEl.insertBefore(row, notesEl.firstChild);
                        editorRow = row;
                        sizeToGrid();
                        input.focus();
                    }
                    async function loadNotes() {
                        const j = await recTry(recApi(
                            recHostId,
                            '/recording/notes?id=' + encodeURIComponent(recId)));
                        if (j.ok) {
                            notes = j.notes || [];
                            notesRev = j.rev || 0;
                        } else if (j.error === 'auth_required') {
                            promptFileHostAuth(recHost(recHostId));
                        }
                        renderNotes();
                    }
                    // Saves are CHAINED: two quick edits would otherwise race
                    // on the same baseRev and the loser's 409 would silently
                    // drop an edit. Each queued save snapshots the CURRENT
                    // list at execution time, so a coalesced save carries
                    // every local edit under a fresh rev.
                    let notesChain = Promise.resolve();
                    function saveNotes() {
                        notesChain = notesChain.then(doSaveNotes,
                                                     doSaveNotes);
                        return notesChain;
                    }
                    async function doSaveNotes() {
                        const j = await recTry(recPost(
                            recHostId, '/recording/notes',
                            { id: recId, baseRev: notesRev, notes: notes }));
                        if (j.ok) {
                            notesRev = j.rev;
                            setStatus('');
                        } else if (j.error === 'auth_required') {
                            // Same shape as the sticky failure below — the list
                            // on screen holds edits the broker does not have —
                            // but this one has a remedy, so offer it.
                            promptFileHostAuth(recHost(recHostId));
                            setStatus('note save failed — sign in to '
                                      + recHostLabel(recHost(recHostId)));
                        } else if (j.error === 'conflict') {
                            // Another player window changed the notes — adopt
                            // the live copy (the user re-applies their edit).
                            notes = j.notes || [];
                            notesRev = j.rev || 0;
                            setStatus('notes changed elsewhere — reloaded');
                            setTimeout(function () { setStatus(''); }, 4000);
                        } else {
                            // Sticky (no timeout): the list on screen holds
                            // edits the broker does NOT have — don't let the
                            // warning quietly vanish while that's true.
                            setStatus('note save failed — edits not saved');
                        }
                        renderNotes();
                        if (libRender) libRender();
                    }
                    noteBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (!recData) return;
                        setPlaying(false);
                        const t = Math.round(pos);
                        openNoteEditor(t, '', function (text) {
                            if (!text) return;
                            notes.push({ t: t, text: text });
                            notes.sort(function (a, b) { return a.t - b.t; });
                            saveNotes();
                        });
                    });

                    // ---- load ---------------------------------------------
                    // A named function, not the one-shot IIFE it used to be:
                    // openAppWindow dedupes by id and only FOCUSES an existing
                    // window, so a player that failed its load (a remote broker
                    // whose token had expired) could never be retried — Play
                    // just re-focused the dead window. _onHostAuth below re-runs
                    // it once that broker is signed into.
                    let loading = false;
                    async function load() {
                        // recData set = this player is already up; a retry must
                        // never build a second Terminal over the live one.
                        if (loading || closed || recData) return;
                        loading = true;
                        setStatus('loading…');
                        try {
                            // Same helper as the download, deadline-free for
                            // the same reason: the player loads the whole
                            // recording body.
                            const r = await fetchRecordingBody(recHostId, recId);
                            if (!r.ok) {
                                if (r.auth) {
                                    promptFileHostAuth(recHost(recHostId));
                                }
                                throw new Error(r.error);
                            }
                            if (closed) return;
                            recData = parseRecording(r.text);
                            duration = Math.max(
                                1, recData.meta.durationMs
                                   || (recData.events.length
                                       ? recData.events[
                                           recData.events.length - 1].t : 1));
                            term = new Terminal({
                                cols: recData.meta.cols || 80,
                                rows: recData.meta.rows || 24,
                                scrollback: 200, cursorBlink: false,
                                disableStdin: true,
                                fontFamily: recData.meta.fontFamily
                                    || TERM_FONT_BASELINE,
                                fontSize: recData.meta.fontSize || 14,
                                theme: { background: '#000000' },
                            });
                            win.term = term;
                            term.open(termHost);
                            sizeToGrid();
                            setStatus('indexing…');
                            kfs = await buildKeyframes(
                                recData,
                                function (done, total) {
                                    setStatus('indexing… '
                                        + Math.round((done / total) * 100)
                                        + '%');
                                });
                            if (closed) return;
                            setStatus('');
                            renderAt(0);
                            syncUi(true);
                            await loadNotes();
                        } catch (e) {
                            if (!closed) {
                                setStatus('load failed: '
                                    + String(e && e.message || e));
                            }
                        } finally {
                            loading = false;
                        }
                    }
                    load();
                    // #46's per-window hook: core fires it on every live window
                    // after a successful login. Only this recording's broker
                    // matters — another host authenticating is not a reason to
                    // retry — and load() self-guards the already-loaded case.
                    win._onHostAuth = function (hostId) {
                        if (hostId === recHostId) load();
                    };

                    win.cleanups.push(function () {
                        closed = true;
                        gen++;
                        playing = false;
                        if (raf !== null) cancelAnimationFrame(raf);
                        try { if (term) term.dispose(); } catch (_) {}
                        win.term = null;
                    });

                    bringToFront(id);
                    return win;
                }

                // ---- library window (singleton, EPHEMERAL) -----------------
                function openLibraryWindow(appData) {
                    const id = String(appData.id);
                    const title = 'Session recordings';
                    const geom = clampGeom(appData.geom
                                           || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color
                                               || defaultColor(id));
                    const chrome = buildAppChrome({
                        id: id, appClass: 'app-reclib', badge: '#rec',
                        geom: geom, color: color, locked: true, title: title,
                    });
                    const dom = chrome.dom;
                    const toolbar = document.createElement('div');
                    toolbar.className = 'app-toolbar reclib-toolbar';
                    const refreshBtn = document.createElement('button');
                    refreshBtn.type = 'button';
                    refreshBtn.textContent = 'Refresh';
                    // #161 broker filter. Hidden entirely while one broker is
                    // configured (the majority setup) so the single-host window
                    // is exactly what it was. Options are rebuilt on every
                    // refresh — brokers are added and removed from the Control
                    // Panel while this window is open.
                    const hostSel = document.createElement('select');
                    hostSel.className = 'reclib-hostsel';
                    hostSel.title = 'which broker to list recordings from';
                    const statusEl = document.createElement('span');
                    statusEl.className = 'reclib-status';
                    toolbar.appendChild(refreshBtn);
                    toolbar.appendChild(hostSel);
                    toolbar.appendChild(statusEl);
                    const listEl = document.createElement('div');
                    listEl.className = 'reclib-body';
                    dom.appendChild(toolbar);
                    dom.appendChild(listEl);
                    addResizeHandles(dom);
                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id: id, sid: 'rec', hostId: 'app',
                        type: 'app', appKind: 'recorder',
                        dom: dom, body: listEl, titleText: chrome.titleText,
                        term: null, fitAddon: null,
                        ws: null, wsOpen: false, termReady: false,
                        minimized: false, disposed: false,
                        geom: geom, name: title, color: color,
                        resizeTimer: null, lastSentDims: null,
                        staleSession: false, authFailed: false,
                        reattachAttempts: 0, reattachAt: 0,
                        lastOpenAt: 0, missingPolls: 0,
                        cleanups: [],
                        tiled: false,
                        floatGeom: appData.floatGeom
                            ? Object.assign({}, appData.floatGeom) : null,
                        locked: true, dirty: false,
                    };
                    windows.set(id, win);
                    wireAppChrome(win, chrome);

                    const appSess = { key: id, sid: 'rec', id: id,
                                      title: title, stale: false,
                                      kind: 'app', hostId: 'app' };
                    sessions.set(id, appSess);
                    const itemsHost = document.getElementById('taskbar-items');
                    if (!itemsHost.querySelector(
                            '.taskbar-item[data-session-id="'
                            + cssEscape(id) + '"]')) {
                        itemsHost.appendChild(buildTaskbarItem(appSess));
                    }
                    updateTaskbarColor(id);
                    updateTaskbarLabel(id);
                    const emptyMsg = document.getElementById('taskbar-empty');
                    if (emptyMsg) emptyMsg.remove();

                    // Which brokers this window lists. '' = all of them.
                    // Deliberately NOT filtered by hostHidden(): "hidden" is a
                    // display mask over a broker's WINDOWS and taskbar items
                    // (focus mode), and dropping its recordings here would also
                    // remove the only way to delete them while it is on.
                    // Documented in help.md rather than inferred.
                    function listHosts() {
                        const all = allHosts();
                        if (!libFilterHostId) return all;
                        const one = all.filter(h => h.id === libFilterHostId);
                        // A filter pinned to a broker that has since been
                        // removed would otherwise show an empty library with no
                        // way back — fall back to all and reset the control.
                        if (one.length) return one;
                        libFilterHostId = '';
                        return all;
                    }
                    // Rebuilt only when the host set actually CHANGED: this runs
                    // on every refresh, including background ones, and blowing
                    // the <select> away mid-interaction would shut the dropdown
                    // under the user. '' is the "all brokers" value, and cannot
                    // collide with a host id: getHosts() drops any entry whose
                    // id is not a NON-EMPTY string.
                    function syncHostSel() {
                        const all = allHosts();
                        hostSel.style.display = all.length > 1 ? '' : 'none';
                        const want = [''].concat(all.map(h => h.id));
                        const have = Array.from(hostSel.options)
                            .map(o => o.value);
                        if (want.length !== have.length
                            || want.some((v, i) => v !== have[i])) {
                            hostSel.innerHTML = '';
                            const optAll = document.createElement('option');
                            optAll.value = '';
                            optAll.textContent = 'All brokers';
                            hostSel.appendChild(optAll);
                            for (const h of all) {
                                const o = document.createElement('option');
                                o.value = h.id;
                                o.textContent = recHostLabel(h);
                                hostSel.appendChild(o);
                            }
                        }
                        hostSel.value = libFilterHostId;
                    }

                    // #161: one GET per broker, in parallel, merged into one
                    // list. Each host is wrapped on its own — recApi resolves an
                    // HTTP failure to {ok:false,error} but still REJECTS on a
                    // transport one (a dead broker, a deadline abort), and with
                    // remotes in the fan-out that is ordinary rather than
                    // exotic. One unreachable broker must cost its own row, not
                    // the whole list. (Before the fan-out this same rejection
                    // used to vanish into an unhandled promise and leave the
                    // window stuck on "loading…" with nothing in the console.)
                    //
                    // opts.prompt: may this refresh open a login overlay?
                    // Only a USER-initiated one may — the window opening, the
                    // Refresh button, a row's Sign in. libRender() repaints
                    // fire from a recording finishing its save and from a note
                    // save, and a password modal popping out of a background
                    // repaint is exactly the modal storm this mod must not add.
                    let refreshGen = 0;
                    async function refresh(opts) {
                        const o = opts || {};
                        const myGen = ++refreshGen;
                        const hosts = listHosts();
                        syncHostSel();
                        statusEl.textContent = 'loading…';
                        const results = await Promise.all(hosts.map(
                            async function (h) {
                                const j = await recTry(
                                    recApi(h.id, '/recordings'));
                                return { hostId: h.id, j: j };
                            }));
                        // A slower earlier refresh must never paint over a
                        // newer one (Refresh spam, or a save landing mid-load).
                        if (win.disposed || myGen !== refreshGen) return;
                        listEl.innerHTML = '';
                        const multi = allHosts().length > 1;
                        const failed = [];
                        // [hostId, rec] pairs. Each broker returns its own list
                        // newest-first; the merge re-sorts across them.
                        let entries = [];
                        for (const res of results) {
                            const j = res.j || {};
                            // Array-check, not a truthiness check: one broker
                            // answering something unexpected under `recordings`
                            // must not break the renderer for the others.
                            if (!j.ok || !Array.isArray(j.recordings)) {
                                failed.push({ hostId: res.hostId,
                                              error: j.error || 'load failed' });
                                continue;
                            }
                            for (const r of j.recordings) {
                                if (r && typeof r === 'object' && r.id) {
                                    entries.push({ hostId: res.hostId, rec: r });
                                }
                            }
                        }
                        // Sorting only when there is something to merge. With a
                        // single source the broker's own order is authoritative
                        // and is passed through untouched — a re-sort could only
                        // reorder it, and startedAt is not sound enough to do
                        // that on (below).
                        if (results.length > 1) {
                            // startedAt is Date.now() on whichever BROWSER made
                            // the recording, so across machines it is an
                            // approximation, not a clock. Sort on it anyway (it
                            // is the only ordering the user thinks in), but
                            // defensively: a missing/NaN/hand-edited value sorts
                            // last instead of scrambling the list, and ties
                            // break on host order then the broker's own
                            // position, so the order is at least STABLE between
                            // repaints.
                            const at = function (e) {
                                const v = e.rec.startedAt;
                                return (typeof v === 'number' && isFinite(v))
                                    ? v : -Infinity;
                            };
                            const hostOrder = new Map(
                                hosts.map((h, i) => [h.id, i]));
                            entries = entries.map((e, i) => ({ e: e, i: i }));
                            entries.sort(function (a, b) {
                                const d = at(b.e) - at(a.e);
                                if (d) return d;
                                const ha = hostOrder.get(a.e.hostId) || 0;
                                const hb = hostOrder.get(b.e.hostId) || 0;
                                return (ha - hb) || (a.i - b.i);
                            });
                            entries = entries.map(x => x.e);
                        }
                        for (const f of failed) {
                            listEl.appendChild(buildHostError(f));
                        }
                        // Only prompt for ONE broker per refresh, and only the
                        // first in host order: promptFileHostAuth force-opens
                        // whenever no overlay is up, so prompting for each
                        // failing host in turn would queue a login storm.
                        if (o.prompt) {
                            const first = failed.find(
                                f => f.error === 'auth_required');
                            if (first) promptFileHostAuth(recHost(first.hostId));
                        }
                        if (!entries.length) {
                            const empty = document.createElement('div');
                            empty.className = 'reclib-empty';
                            // "press ⏺ to record one" is only true of the local
                            // broker — captures always save HERE — so it would
                            // be a lie under a remote-only filter.
                            const onlyRemote = hosts.length === 1
                                && hosts[0].id !== 'local';
                            empty.textContent = failed.length
                                ? 'no recordings from the brokers that answered'
                                : (onlyRemote
                                   ? ('no recordings on '
                                      + recHostLabel(hosts[0]))
                                   : ('no recordings yet — press ⏺ on a '
                                      + 'terminal title bar to record one'));
                            listEl.appendChild(empty);
                        }
                        statusEl.textContent =
                            (failed.length && failed.length === results.length)
                                ? ('load failed: ' + failed[0].error) : '';
                        // #151: how long each chain is, so a rolled recording
                        // reads as "part 2/3" instead of three identically-named
                        // rows. One pass over the whole list, not per row.
                        //
                        // The HIGHEST seg, not the number of rows: delete part 1
                        // of a three-part run and a row count would render the
                        // survivors as "part 2/2" and "part 3/2", and a lone
                        // surviving part 3 would lose its label entirely. A max
                        // says what the chain's length was, which stays true as
                        // segments are deleted. An ordinary recording carries a
                        // series id too, so a max of 1 must not grow a "part 1".
                        //
                        // #161: keyed per BROKER. Series ids are minted client-
                        // side, so two brokers can hold unrelated chains under
                        // the same one, and merging them would invent parts.
                        const segMax = new Map();
                        for (const e of entries) {
                            const r = e.rec;
                            if (!r.series || !r.seg) continue;
                            const k = recKey(e.hostId, r.series);
                            segMax.set(k, Math.max(segMax.get(k) || 0, r.seg));
                        }
                        for (const e of entries) {
                            listEl.appendChild(buildRow(e, segMax, multi));
                        }
                    }
                    // A broker that did not answer. Its own row rather than a
                    // status line: with several brokers the status line can only
                    // report one of them, and a silently-shorter list is the
                    // failure mode this whole feature exists to remove.
                    function buildHostError(f) {
                        const host = recHost(f.hostId);
                        const row = document.createElement('div');
                        row.className = 'reclib-row reclib-hosterr';
                        const main = document.createElement('div');
                        main.className = 'reclib-main';
                        const name = document.createElement('div');
                        name.className = 'reclib-name';
                        name.textContent = recHostLabel(host);
                        const info = document.createElement('div');
                        info.className = 'reclib-info';
                        info.textContent = f.error === 'auth_required'
                            ? 'password required' : f.error;
                        main.appendChild(name);
                        main.appendChild(info);
                        row.appendChild(main);
                        if (f.error === 'auth_required' && host) {
                            const btns = document.createElement('div');
                            btns.className = 'reclib-btns';
                            const signin = document.createElement('button');
                            signin.type = 'button';
                            signin.className = 'reclib-btn';
                            signin.textContent = 'Sign in';
                            signin.addEventListener('mousedown',
                                function (e) { e.stopPropagation(); });
                            signin.addEventListener('click', function (e) {
                                e.stopPropagation();
                                promptFileHostAuth(recHost(f.hostId));
                            });
                            btns.appendChild(signin);
                            row.appendChild(btns);
                        }
                        return row;
                    }
                    function buildRow(entry, segMax, multi) {
                        const r = entry.rec;
                        const hostId = entry.hostId;
                        const row = document.createElement('div');
                        row.className = 'reclib-row';
                        const name = document.createElement('div');
                        name.className = 'reclib-name';
                        name.textContent = r.title || r.id;
                        name.title = r.id;
                        const infoLine = document.createElement('div');
                        infoLine.className = 'reclib-info';
                        // Which broker STORES this recording (#161) — shown only
                        // when there is more than one, so the single-broker
                        // window is unchanged. The #103 per-host identity colour
                        // rides the dot, never the label text.
                        if (multi) {
                            const host = recHost(hostId);
                            const tag = document.createElement('span');
                            tag.className = 'reclib-host';
                            const dot = document.createElement('span');
                            dot.className = 'reclib-host-dot';
                            const c = host && strictHex(host.color);
                            if (c) dot.style.background = c;
                            tag.appendChild(dot);
                            tag.appendChild(document.createTextNode(
                                recHostLabel(host)));
                            tag.title = 'stored on ' + recHostLabel(host);
                            infoLine.appendChild(tag);
                        }
                        // The part marker rides the INFO line, not the name:
                        // .reclib-name is nowrap + ellipsis, and a session title
                        // long enough to truncate (it carries the running command
                        // line) would clip the one label that says this recording
                        // is only part of a run.
                        const parts = (r.series && segMax)
                            ? (segMax.get(recKey(hostId, r.series)) || 0) : 0;
                        if (parts > 1 && r.seg) {
                            const seg = document.createElement('span');
                            seg.className = 'reclib-part';
                            seg.textContent = 'part ' + r.seg + '/' + parts;
                            seg.title = 'segment ' + r.seg + ' of a recording '
                                + 'that rolled over at the size cap';
                            infoLine.appendChild(seg);
                        }
                        const when = r.startedAt
                            ? new Date(r.startedAt).toLocaleString() : '';
                        // appended, NOT assigned to textContent: the part marker
                        // above is already a child of this line.
                        infoLine.appendChild(document.createTextNode(
                            when
                            + '  ·  ' + fmtClock(r.durationMs || 0)
                            + '  ·  ' + (r.cols || '?') + '×' + (r.rows || '?')
                            + '  ·  '));
                        // #159: `size` still means the RECORDING's size — what
                        // it was before compression and what a download gives
                        // you — so the column reads the same across a library
                        // holding both encodings. The on-disk footprint is a
                        // separate number and belongs in a tooltip, not in a
                        // column that would then mean two different things
                        // depending on when the recording was made.
                        const sizeEl = document.createElement('span');
                        sizeEl.textContent = fmtSize(r.size || 0);
                        if (r.diskSize != null && r.diskSize !== r.size) {
                            sizeEl.title = fmtSize(r.diskSize) + ' on disk'
                                + (r.enc === 'gzip' ? ' (gzip)' : '');
                        }
                        infoLine.appendChild(sizeEl);
                        infoLine.appendChild(document.createTextNode(
                            r.notesCount
                                ? ('  ·  ' + r.notesCount + ' note'
                                   + (r.notesCount === 1 ? '' : 's'))
                                : ''));
                        const btns = document.createElement('div');
                        btns.className = 'reclib-btns';
                        const play = document.createElement('button');
                        play.type = 'button';
                        play.textContent = '▶ Play';
                        play.addEventListener('click', function (e) {
                            e.stopPropagation();
                            // The window id carries the BROKER too (#161): ids
                            // are only unique per broker, so without it playing
                            // recording X from host B would just re-focus the
                            // already-open player for X from host A. Both
                            // components are encoded — openAppWindow keys on the
                            // whole string, and a host id is free-form.
                            openAppWindow({
                                id: 'app:recplay:'
                                    + encodeURIComponent(hostId) + ':'
                                    + encodeURIComponent(r.id),
                                appKind: 'recplayer',
                                hostId: hostId, recId: r.id, meta: r,
                            });
                        });
                        const dl = document.createElement('button');
                        dl.type = 'button';
                        dl.textContent = '⬇';
                        dl.title = 'download';
                        dl.addEventListener('click', function (e) {
                            e.stopPropagation();
                            downloadRecording(hostId, r.id, function (msg) {
                                // The library's status line while the window is
                                // still up; a notice once it has been closed.
                                if (win.disposed) {
                                    showNotice(msg, { sticky: true });
                                } else {
                                    statusEl.textContent = msg;
                                }
                            });
                        });
                        const del = document.createElement('button');
                        del.type = 'button';
                        del.textContent = '✕';
                        del.title = 'delete recording';
                        let armed = false, disarm = null;
                        del.addEventListener('click', async function (e) {
                            e.stopPropagation();
                            // Two-click confirm: the first click arms for 3s.
                            if (!armed) {
                                armed = true;
                                del.textContent = 'sure?';
                                del.classList.add('armed');
                                disarm = setTimeout(function () {
                                    armed = false;
                                    del.textContent = '✕';
                                    del.classList.remove('armed');
                                }, 3000);
                                return;
                            }
                            clearTimeout(disarm);
                            // recTry: a delete against a REMOTE broker can fail
                            // in transport, and this listener is async — the
                            // rejection had nowhere to go but an unhandled
                            // promise, leaving the row armed and the user with
                            // no idea whether the recording is gone.
                            const j = await recTry(recPost(
                                hostId, '/recording/delete', { id: r.id }));
                            if (win.disposed) return;
                            if (j.ok) refresh();
                            else {
                                if (j.error === 'auth_required') {
                                    promptFileHostAuth(recHost(hostId));
                                }
                                statusEl.textContent = 'delete failed: '
                                    + (j.error || '');
                            }
                        });
                        for (const b of [play, dl, del]) {
                            b.classList.add('reclib-btn');
                            b.addEventListener('mousedown',
                                               function (e) { e.stopPropagation(); });
                            btns.appendChild(b);
                        }
                        const main = document.createElement('div');
                        main.className = 'reclib-main';
                        main.appendChild(name);
                        main.appendChild(infoLine);
                        row.appendChild(main);
                        row.appendChild(btns);
                        return row;
                    }
                    refreshBtn.addEventListener('mousedown',
                                                function (e) { e.stopPropagation(); });
                    refreshBtn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        refresh({ prompt: true });
                    });
                    hostSel.addEventListener('mousedown',
                                             function (e) { e.stopPropagation(); });
                    hostSel.addEventListener('change', function () {
                        libFilterHostId = hostSel.value;
                        refresh({ prompt: true });
                    });
                    // The BACKGROUND repaint (a recording finished saving, a
                    // note was saved). Trailing-edge coalesced: with one broker
                    // this was one cheap local GET, but it now costs one per
                    // broker, and the events that drive it arrive in bursts —
                    // the auto-record off switch stops every terminal at once,
                    // and each of those saves lands its own repaint. Never
                    // prompts for a password (see refresh).
                    let bgTimer = null;
                    const bgRefresh = function () {
                        if (bgTimer !== null) return;
                        bgTimer = setTimeout(function () {
                            bgTimer = null;
                            if (!win.disposed) refresh({ prompt: false });
                        }, 250);
                    };
                    libRender = bgRefresh;
                    win.cleanups.push(function () {
                        if (bgTimer !== null) clearTimeout(bgTimer);
                        if (libRender === bgRefresh) libRender = null;
                    });
                    // #46's per-window hook: heal the list in place once a
                    // broker is signed into, instead of leaving its error row
                    // up until someone presses Refresh.
                    win._onHostAuth = function () {
                        refresh({ prompt: false });
                    };

                    refresh({ prompt: true });
                    finishWindowPlacement(win);
                    return win;
                }

                function launchLibrary() {
                    return openAppWindow({ id: LIB_WIN_ID,
                                           appKind: 'recorder' });
                }
                ctx.registerWindowKind({
                    appKind: 'recorder',
                    factory: function (d) { return openLibraryWindow(d); },
                    menu: {
                        label: 'Session recorder',
                        iconKey: 'recorder',
                        launch: function () { return launchLibrary(); },
                    },
                });
                // Player windows: EPHEMERAL, opened only from the library —
                // no (+) menu entry (menu omitted), no serialize.
                ctx.registerWindowKind({
                    appKind: 'recplayer',
                    factory: function (d) { return openPlayerWindow(d); },
                });

                // Teardown — LIFO runs this FIRST (before the kind
                // registrations are removed), closing live windows while their
                // kinds are still registered, same reasoning as clipboard.
                // Any live recording stops (and saves) via its disposer.
                ctx.onUnload(function () {
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app'
                            && (w.appKind === 'recorder'
                                || w.appKind === 'recplayer')) {
                            closeWindow(w.id);
                        }
                    }
                    for (const fn of Array.from(disposers)) {
                        try { fn(); } catch (_) {}
                    }
                    disposers.clear();
                    window.removeEventListener('beforeunload', onBeforeUnload);
                });
            },
        });
