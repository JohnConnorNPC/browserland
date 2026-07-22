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
            defaultEnabled: true,   // inert until ⏺ is clicked — records nothing on its own
            tiers: ['window'],
            init: function (ctx) {
                if (!ctx.windows || !ctx.registerWindowKind) return;

                const LIB_WIN_ID = 'app:recorder';
                const REC_CAP_BYTES = 50 * 1024 * 1024;  // auto-stop ceiling
                const KF_BYTES = 192 * 1024;             // keyframe every N output bytes…
                const KF_MS = 2000;                      // …or N ms of recorded time
                const CHUNK_RAW = 2 * 1024 * 1024;       // upload chunk (decoded)
                const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];

                const active = new Map();     // win.id -> recording state
                const disposers = new Set();  // live per-terminal UI teardowns
                let libRender = null;         // live library window repaint

                // ---- local-broker HTTP (the token rides an Authorization
                // header, never the URL -- see hostFetch, #144) -------------
                async function recApi(path, opts) {
                    const r = await hostFetch(localHost(), path, opts);
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
                        const r = await hostFetch(
                            localHost(),
                            '/recording?id=' + encodeURIComponent(recId));
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
                function syncUnloadGuard() {
                    if (active.size === 1) {
                        window.addEventListener('beforeunload', onBeforeUnload);
                    } else if (active.size === 0) {
                        window.removeEventListener('beforeunload', onBeforeUnload);
                    }
                }

                function startRecording(win, info, ui) {
                    if (!win || win.disposed || !win.term || active.has(win.id)) return;
                    const t0 = performance.now();
                    const rec = {
                        win: win, ui: ui,
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
                    };
                    const now = function () {
                        return Math.max(0, Math.round(performance.now() - t0));
                    };
                    // Seed with the CURRENT screen so playback starts from
                    // what the terminal looked like at ⏺, not a blank grid.
                    // Degrades to a blank start if the serialize addon didn't
                    // load (same fallback as the playback keyframes).
                    try {
                        const SerCls = (typeof SerializeAddon !== 'undefined'
                                        && SerializeAddon
                                        && SerializeAddon.SerializeAddon)
                                       || null;
                        if (SerCls) {
                            const ser = new SerCls();
                            win.term.loadAddon(ser);
                            const snap = ser.serialize({ scrollback: 0 });
                            ser.dispose();
                            if (snap) {
                                rec.events.push({ t: 0, k: 'o',
                                                  d: bytesToB64(
                                                      encoder.encode(snap)) });
                                rec.bytes += snap.length;
                            }
                        }
                    } catch (_) {}
                    const pushOut = function (u8) {
                        if (rec.stopped) return;
                        // A ws swap since the last chunk = a disconnect healed
                        // by a reattach snapshot — mark the gap on the timeline.
                        if (win.ws !== rec.ws) {
                            rec.events.push({ t: now(), k: 'g' });
                            rec.ws = win.ws;
                        }
                        rec.bytes += u8.length;
                        rec.events.push({ t: now(), k: 'o', d: bytesToB64(u8) });
                        if (rec.bytes > REC_CAP_BYTES) {
                            stopRecording(win, 'size cap');
                        }
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
                            }
                        } catch (_) {}
                        return rec.rawResize.call(win.term, cols, rows);
                    };
                    win.term.write = rec.wrapWrite;
                    win.term.resize = rec.wrapResize;
                    // Input MARKERS only: timestamp + byte count, never content
                    // (typed passwords are unechoed — content would leak them).
                    rec.inputDisp = win.term.onData(function (d) {
                        rec.events.push({ t: now(), k: 'i',
                                          n: (d && d.length) || 0 });
                    });
                    rec.ticker = setInterval(function () {
                        ui.setLabel(fmtClock(now()));
                    }, 1000);
                    active.set(win.id, rec);
                    syncUnloadGuard();
                    ui.setRecording(true);
                    ui.setLabel('0:00');
                }

                function stopRecording(win, reason) {
                    const rec = active.get(win.id);
                    if (!rec || rec.stopped) return;
                    rec.stopped = true;
                    active.delete(win.id);
                    syncUnloadGuard();
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
                    saveRecording(rec).then(function (res) {
                        rec.ui.setLabel(res.ok ? 'saved ✓'
                                               : 'save failed');
                        if (libRender) libRender();
                        setTimeout(function () { rec.ui.setLabel(''); }, 4000);
                    });
                }

                async function saveRecording(rec) {
                    const meta = {
                        v: 1, title: rec.title,
                        cols: rec.cols, rows: rec.rows,
                        startedAt: rec.startedAt,
                        durationMs: rec.durationMs,
                        fontFamily: rec.fontFamily, fontSize: rec.fontSize,
                        events: rec.events.length, bytes: rec.bytes,
                    };
                    const lines = [JSON.stringify(meta)];
                    for (const ev of rec.events) lines.push(JSON.stringify(ev));
                    const payload = encoder.encode(lines.join('\n') + '\n');
                    let recId = null;
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
                        return await recPost('/recording/commit',
                                             { recording_id: recId, meta: meta });
                    } catch (e) {
                        // Best-effort cleanup; the ORIGINAL failure is what
                        // gets reported (mirrors the upload_abort contract).
                        if (recId) {
                            try {
                                recPost('/recording/abort',
                                        { recording_id: recId });
                            } catch (_) {}
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
                        if (active.has(win.id)) stopRecording(win, 'user');
                        else startRecording(win, info, ui);
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
                        btn.removeEventListener('mousedown', stopProp);
                        btn.removeEventListener('click', onClick);
                        btn.remove();
                        label.remove();
                    };
                    disposers.add(teardown);
                    info.onDispose(teardown);
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
                    const title = 'Playback — '
                        + (meta0.title || recId);
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
                            const r = await hostFetch(
                                localHost(),
                                '/recording?id='
                                + encodeURIComponent(recId));
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

                    async function refresh() {
                        statusEl.textContent = 'loading…';
                        const j = await recApi('/recordings');
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
                        for (const r of recs) {
                            listEl.appendChild(buildRow(r));
                        }
                    }
                    function buildRow(r) {
                        const row = document.createElement('div');
                        row.className = 'reclib-row';
                        const name = document.createElement('div');
                        name.className = 'reclib-name';
                        name.textContent = r.title || r.id;
                        name.title = r.id;
                        const infoLine = document.createElement('div');
                        infoLine.className = 'reclib-info';
                        const when = r.startedAt
                            ? new Date(r.startedAt).toLocaleString() : '';
                        infoLine.textContent = when
                            + '  ·  ' + fmtClock(r.durationMs || 0)
                            + '  ·  ' + (r.cols || '?') + '×' + (r.rows || '?')
                            + '  ·  ' + fmtSize(r.size || 0)
                            + (r.notesCount
                               ? ('  ·  ' + r.notesCount + ' note'
                                  + (r.notesCount === 1 ? '' : 's'))
                               : '');
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
