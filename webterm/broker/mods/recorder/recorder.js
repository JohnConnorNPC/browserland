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
            init: function (ctx) {
                if (!ctx.windows || !ctx.registerWindowKind) return;

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

                // ---- local-broker HTTP (the token rides an Authorization
                // header, never the URL -- see hostFetch, #144) -------------
                // Per-route deadlines. These are all JSON control calls on the
                // local broker, but they are not uniformly cheap: a chunk PUT
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
                async function recApi(path, opts) {
                    const o = Object.assign({}, opts || {});
                    if (o.timeoutMs === undefined) {
                        // The query string ('/recording/notes?id=...') is not
                        // part of the route key.
                        const route = path.split('?')[0];
                        o.timeoutMs = REC_TIMEOUT_MS.has(route)
                            ? REC_TIMEOUT_MS.get(route) : REC_TIMEOUT_DEFAULT_MS;
                    }
                    const r = await hostFetch(localHost(), path, o);
                    let j = null;
                    try { j = await r.json(); } catch (_) {}
                    if (!j || typeof j !== 'object') {
                        return { ok: false, error: 'HTTP ' + r.status };
                    }
                    if (j.ok === undefined) j.ok = r.ok;
                    return j;
                }
                function recPost(path, body) {
                    return recApi(path, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body),
                    });
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
                const dlBusy = new Set();   // rec ids with a download in flight
                function dlReport(report, msg) {
                    // A download outlives its window, so the caller's status
                    // line may be detached by the time we fail. The notice host
                    // is always in the document.
                    if (report) {
                        try { report(msg); return; } catch (_) {}
                    }
                    showNotice(msg, { sticky: true });
                }
                async function downloadRecording(recId, report) {
                    // Repeat clicks used to be free (the browser streamed each
                    // one to disk); now each would buffer the whole file, so a
                    // double-click on a big recording doubles the memory.
                    if (dlBusy.has(recId)) return;
                    dlBusy.add(recId);
                    try {
                        // NO deadline: this streams the recording itself, up to
                        // MAX_RECORDING_BYTES (256 MiB). Any fixed timer is a
                        // guess about the user's link speed, and losing a large
                        // download to one is far worse than waiting for it.
                        const r = await hostFetch(
                            localHost(),
                            '/recording?id=' + encodeURIComponent(recId),
                            { timeoutMs: 0 });
                        if (!r.ok) {
                            let j = null;
                            try { j = await r.json(); } catch (_) {}
                            const err = (j && j.error) || ('HTTP ' + r.status);
                            // Before this, a stale token (the broker restarted
                            // and minted a new one) SAVED the 401 JSON body as
                            // <id>.blrec — a 36-byte "recording" with no error
                            // shown anywhere. Re-open the login prompt instead.
                            if (err === 'auth_required') {
                                promptFileHostAuth(localHost());
                            }
                            dlReport(report, 'download failed: ' + err);
                            return;
                        }
                        // blob(), not text(): the bytes stay opaque and the
                        // Blob inherits the response's application/octet-stream,
                        // where text() would decode UTF-8 into a UTF-16 string
                        // and double peak memory for nothing.
                        const url = URL.createObjectURL(await r.blob());
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
                        // fetch() REJECTS on a network failure (broker stopped,
                        // tailnet dropped) rather than resolving with ok=false.
                        // Unhandled, that would be a silent no-op — strictly
                        // worse than the failed entry the download manager used
                        // to show.
                        dlReport(report,
                                 'download failed: '
                                 + String((e && e.message) || e));
                    } finally {
                        dlBusy.delete(recId);
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
                    try {
                        const b = await recPost('/recording/begin', {});
                        if (!b.ok) return b;
                        recId = b.recording_id;
                        for (let off = 0; off < payload.length; off += CHUNK_RAW) {
                            const part = payload.subarray(off, off + CHUNK_RAW);
                            const c = await recPost('/recording/chunk', {
                                recording_id: recId, offset: off,
                                content_b64: bytesToB64(part),
                            });
                            if (!c.ok) throw new Error(c.error || 'chunk');
                        }
                        committing = true;
                        return await recPost('/recording/commit',
                                             { recording_id: recId, meta: meta });
                    } catch (e) {
                        // Best-effort cleanup; the ORIGINAL failure is what
                        // gets reported (mirrors the upload_abort contract).
                        if (recId && !committing) {
                            // The sync try/catch never saw this promise's
                            // REJECTION — recApi rejects on a transport failure
                            // (and now on a deadline abort), so a failed cleanup
                            // became an unhandled rejection in the console. Catch
                            // it on the promise; cleanup stays fire-and-forget so
                            // the ORIGINAL failure is what gets reported.
                            recPost('/recording/abort',
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
                    // The stored title is the plain session name for every
                    // segment; the part number is DERIVED from meta.seg here
                    // rather than baked into the title, so `seg` stays the one
                    // source of truth for a chain's ordering.
                    const title = 'Playback — '
                        + (meta0.title || recId)
                        + (meta0.seg > 1 ? (' (part ' + meta0.seg + ')') : '');
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
                        downloadRecording(recId, function (msg) {
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
                        const j = await recApi('/recording/notes?id='
                                               + encodeURIComponent(recId));
                        if (j.ok) {
                            notes = j.notes || [];
                            notesRev = j.rev || 0;
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
                        const j = await recPost('/recording/notes', {
                            id: recId, baseRev: notesRev, notes: notes,
                        });
                        if (j.ok) {
                            notesRev = j.rev;
                            setStatus('');
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
                    (async function () {
                        setStatus('loading…');
                        try {
                            // No deadline, same reason as downloadRecording:
                            // the player loads the whole recording body.
                            const r = await hostFetch(
                                localHost(),
                                '/recording?id='
                                + encodeURIComponent(recId),
                                { timeoutMs: 0 });
                            if (!r.ok) throw new Error('HTTP ' + r.status);
                            const text = await r.text();
                            if (closed) return;
                            recData = parseRecording(text);
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
                        }
                    })();

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
                    const statusEl = document.createElement('span');
                    statusEl.className = 'reclib-status';
                    toolbar.appendChild(refreshBtn);
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

                    // recApi resolves to {ok:false,error} for an HTTP failure but
                    // still REJECTS on a transport one (a dead broker, a deadline
                    // abort) — and every caller of refresh() is a bare listener or
                    // an un-awaited call, so that rejection used to vanish into an
                    // unhandled promise, leaving the window stuck on "loading…"
                    // forever with nothing on screen or in the console. Catch it
                    // and say so in the same status line as every other failure.
                    async function refresh() {
                        statusEl.textContent = 'loading…';
                        let j;
                        try {
                            j = await recApi('/recordings');
                        } catch (e) {
                            if (win.disposed) return;
                            statusEl.textContent = 'load failed: '
                                + String((e && e.message) || e);
                            return;
                        }
                        if (win.disposed) return;
                        statusEl.textContent = j.ok ? ''
                            : ('load failed: ' + (j.error || ''));
                        listEl.innerHTML = '';
                        const recs = (j.ok && j.recordings) || [];
                        if (!recs.length) {
                            const empty = document.createElement('div');
                            empty.className = 'reclib-empty';
                            empty.textContent = 'no recordings yet — press ⏺ '
                                + 'on a terminal title bar to record one';
                            listEl.appendChild(empty);
                            return;
                        }
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
                        const segMax = new Map();
                        for (const r of recs) {
                            if (!r.series || !r.seg) continue;
                            segMax.set(r.series,
                                       Math.max(segMax.get(r.series) || 0,
                                                r.seg));
                        }
                        for (const r of recs) {
                            listEl.appendChild(buildRow(r, segMax));
                        }
                    }
                    function buildRow(r, segMax) {
                        const row = document.createElement('div');
                        row.className = 'reclib-row';
                        const name = document.createElement('div');
                        name.className = 'reclib-name';
                        name.textContent = r.title || r.id;
                        name.title = r.id;
                        const infoLine = document.createElement('div');
                        infoLine.className = 'reclib-info';
                        // The part marker rides the INFO line, not the name:
                        // .reclib-name is nowrap + ellipsis, and a session title
                        // long enough to truncate (it carries the running command
                        // line) would clip the one label that says this recording
                        // is only part of a run.
                        const parts = (r.series && segMax)
                            ? (segMax.get(r.series) || 0) : 0;
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
                            openAppWindow({
                                id: 'app:recplay:' + r.id,
                                appKind: 'recplayer',
                                recId: r.id, meta: r,
                            });
                        });
                        const dl = document.createElement('button');
                        dl.type = 'button';
                        dl.textContent = '⬇';
                        dl.title = 'download';
                        dl.addEventListener('click', function (e) {
                            e.stopPropagation();
                            downloadRecording(r.id, function (msg) {
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
                            const j = await recPost('/recording/delete',
                                                    { id: r.id });
                            if (j.ok) refresh();
                            else {
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
                        refresh();
                    });
                    libRender = refresh;
                    win.cleanups.push(function () {
                        if (libRender === refresh) libRender = null;
                    });

                    refresh();
                    if (findKeyInLayout(id)) placeWindowTiled(win);
                    else bringToFront(id);
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
