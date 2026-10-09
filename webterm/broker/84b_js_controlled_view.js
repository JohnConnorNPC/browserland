        // ---- controlled from another device (#239) ---------------------------
        // An outside client (#238: first user Notifier, on a phone) can claim
        // one terminal's input and size. The desktop window for it must stop
        // driving it, say who has it, and give it back in one click. So while
        // a terminal is controlled its window is greyed, kept exactly where it
        // is (column slot or float box, chip, title, last frame) and holds NO
        // socket: attachWebSocket (73) refuses to dial while win.controlled is
        // set, which is the one guard behind every dial path (openWindow's
        // tail, detachGateAttach, reattachWindow and both re-dial loops). No
        // socket means no output, no snapshot repaint, no xterm query replies
        // and no `resized` snap; isResizable (73) refuses it as well.
        //
        // The state is per terminal, in terminalControl (64), keyed like the
        // window '<hostId>:<sid>'. Four sources write it, all through
        // applyControlView: the window's own /ws `control` frame, the
        // `/control` `terminal_control` push, the /sessions poll row and the
        // Take back POST's answer. Which of them wins is controlViewAccept:
        //   - a /ws frame, a push or a POST answer applies iff its gen is
        //     newer than the record's (gen rises within one broker run);
        //   - a poll row applies iff that poll was SENT after the record was
        //     last written, whatever its gen: the broker answered from a
        //     state at least that new. That is also what heals a missed push
        //     and a broker restart (whose gens are re-seeded from the clock);
        //   - a /control reconnect forgets that host's gens
        //     (resetControlBaselines), so the next view per key is accepted.
        // A free poll row carries no gen: over a free record it keeps that
        // record's gen; over a controlled one it is recorded as gen null,
        // "no baseline", so the next view of any gen is accepted.
        //
        // Declarations only: nothing below runs at evaluation time.

        function controlKey(hostId, sid) {
            return String(hostId) + ':' + String(sid);
        }

        // One view from any source, checked, or null (which changes nothing).
        // A free view has no label/since; a poll's free row has no gen. A label
        // is 1..64 code points (the broker's rule, #238), shown via
        // textContent only.
        function normalizeControlView(raw, src) {
            if (!raw || typeof raw !== 'object') return null;
            const state = raw.state;
            if (state !== 'free' && state !== 'controlled') return null;
            const gen = raw.gen;
            const noGen = gen === null && src === 'poll' && state === 'free';
            if (!noGen && !(Number.isSafeInteger(gen) && gen >= 0)) return null;
            if (state === 'free') {
                return { state: 'free', label: null, since: null, gen: gen };
            }
            const label = raw.label;
            if (typeof label !== 'string') return null;
            const points = Array.from(label).length;
            if (points < 1 || points > 64) return null;
            const since = (typeof raw.since === 'number'
                           && Number.isFinite(raw.since)) ? raw.since : null;
            return { state: 'controlled', label: label, since: since, gen: gen };
        }

        // Apply-or-ignore, given the key's current record (or null). `sentAt`
        // is the performance.now() at which the poll carrying a row was sent.
        function controlViewAccept(rec, view, src, sentAt) {
            if (!rec) return true;
            if (src === 'poll') {
                return typeof sentAt === 'number' && sentAt > rec.at;
            }
            if (rec.gen === null) return true;
            return view.gen > rec.gen;
        }

        // A new /control socket to a host may be a new broker run: forget the
        // gens recorded for that host's terminals.
        function resetControlBaselines(hostId) {
            const prefix = String(hostId) + ':';
            for (const k of Object.keys(terminalControl)) {
                if (k.slice(0, prefix.length) === prefix) {
                    terminalControl[k].gen = null;
                }
            }
        }

        // THE writer of terminalControl. Returns true when the view was
        // accepted (recorded), whether or not anything visible changed.
        // Transitions are against the window's own win.controlled, so an
        // unchanged view (the same poll row every tick, a push the poll
        // already delivered) changes nothing and fires nothing:
        //   free -> free                 record only, never a reattach
        //   free -> controlled           enter: close the socket, grey, overlay
        //   controlled -> same gen+label record only
        //   controlled -> new gen/label  relabel: a NEW frozen win.controlled
        //   controlled -> free           leave: clear FIRST, then reattach
        // A key with no open window (a chip only) compares records instead.
        function applyControlView(key, raw, src, sentAt) {
            key = String(key);
            const view = normalizeControlView(raw, src);
            if (!view) return false;
            const rec = terminalControl[key] || null;
            if (!controlViewAccept(rec, view, src, sentAt)) return false;
            terminalControl[key] = {
                state: view.state, label: view.label, since: view.since,
                gen: view.gen, at: performance.now(),
            };
            const win = windows.get(key);
            let changed;
            if (win && !win.disposed && win.type !== 'app') {
                changed = transitionControlledWindow(win, view);
            } else {
                changed = !rec || rec.state !== view.state
                    || rec.label !== view.label
                    || (view.state === 'controlled' && rec.gen !== view.gen);
            }
            if (changed) controlStateChanged(key);
            return true;
        }

        // A /sessions row (75's merged record, `sentAt` = when its poll left).
        // A row from a broker without `controller` says nothing. A free row
        // has no gen: it keeps the one already recorded when the record was
        // free too, else records null ("no baseline").
        function applyControlRow(key, sess, sentAt) {
            if (!sess || !sess.controllerKnown) return false;
            const c = sess.controller;
            if (c && typeof c === 'object') {
                return applyControlView(key, { state: 'controlled', label: c.label,
                                               since: c.since, gen: c.gen },
                                        'poll', sentAt);
            }
            const rec = terminalControl[String(key)];
            const gen = (rec && rec.state === 'free') ? rec.gen : null;
            return applyControlView(key, { state: 'free', label: null,
                                           since: null, gen: gen }, 'poll', sentAt);
        }

        function transitionControlledWindow(win, view) {
            const cur = win.controlled || null;
            if (view.state === 'free') {
                if (!cur) return false;
                leaveControlled(win);
                return true;
            }
            if (cur && cur.gen === view.gen && cur.label === view.label) return false;
            const next = Object.freeze({ label: view.label, since: view.since,
                                         gen: view.gen });
            if (cur) {
                win.controlled = next;
                paintControlOverlay(win);
            } else {
                enterControlled(win, next);
            }
            return true;
        }

        // Socket first, then the look: from here on nothing this window does
        // reaches the PTY (no input, no resize, no query reply).
        function enterControlled(win, c) {
            win.controlled = c;
            _closeWinSocket(win);
            if (win.resizeTimer) { clearTimeout(win.resizeTimer); win.resizeTimer = null; }
            win.dom.classList.add('controlled');
            paintControlOverlay(win);
            if (frontId === win.id) focusWin(win);
        }

        // Clear FIRST: attachWebSocket and isResizable both refuse a controlled
        // window, so the reattach (attach, `resized`, snapshot) and the forced
        // initial resize from onopen's maybeSendInitialResize only happen
        // because win.controlled is already null. A window that is out in its
        // own browser window (#226) stays detached: its child owns the PTY and
        // runs this same code on its own page.
        function leaveControlled(win) {
            win.controlled = null;
            win._takingBack = false;
            win.dom.classList.remove('controlled');
            if (win.ctlOverlay) {
                try { win.ctlOverlay.remove(); } catch (_) {}
            }
            win.ctlOverlay = win.ctlLabel = win.ctlSince = win.ctlBtn = null;
            if (win.detached) return;
            win.reattachAttempts = 0;
            win.reattachAt = 0;
            reattachWindow(win);
            if (frontId === win.id) focusWin(win);
        }

        // 24-hour hh:mm, local time; '' for an unknown `since`.
        function controlSinceText(since) {
            if (typeof since !== 'number' || !Number.isFinite(since)) return '';
            const d = new Date(since);
            const p = (n) => (n < 10 ? '0' : '') + n;
            return p(d.getHours()) + ':' + p(d.getMinutes());
        }

        // Over the body only (the title bar stays usable). Built once per
        // entry, repainted in place on a relabel. Every string is textContent:
        // the label is the controller's own text.
        function paintControlOverlay(win) {
            const c = win.controlled;
            if (!c) return;
            if (!win.ctlOverlay) {
                const ov = document.createElement('div');
                ov.className = 'term-ctl-overlay';
                ov.tabIndex = -1;
                const box = document.createElement('div');
                box.className = 'term-ctl-box';
                const msg = document.createElement('div');
                msg.className = 'term-ctl-msg';
                msg.appendChild(document.createTextNode('Controlled from '));
                const who = document.createElement('b');
                who.className = 'term-ctl-label';
                msg.appendChild(who);
                const since = document.createElement('span');
                since.className = 'term-ctl-since';
                msg.appendChild(since);
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'term-ctl-take';
                btn.textContent = 'Take back control';
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    takeBackControl(win);
                });
                box.appendChild(msg);
                box.appendChild(btn);
                ov.appendChild(box);
                win.body.appendChild(ov);
                win.ctlOverlay = ov;
                win.ctlLabel = who;
                win.ctlSince = since;
                win.ctlBtn = btn;
            }
            win.ctlLabel.textContent = c.label;
            const at = controlSinceText(c.since);
            win.ctlSince.textContent = at ? ' since ' + at : '';
            win.ctlBtn.disabled = !!win._takingBack;
        }

        // Take back control: POST /session/control {id, op:'release', gen,
        // clientId} to the terminal's own broker (#238). The answer is applied
        // BY KEY, like every other source, so a window closed and reopened
        // meanwhile is still told; the captured `win` is only its button.
        //   200 {ok, ...free view}        -> leave
        //   409 stale_gen {...live view}  -> adopt it: a new controller
        //                                    relabels, a free one leaves
        //   409 not_active                -> another browser holds the lease:
        //                                    show the existing Become active
        //   404 unknown_session           -> the terminal is gone: close
        //   anything else                 -> say so; nothing changes
        // Closing or terminating a controlled window sends nothing: closing
        // does not release.
        async function takeBackControl(win) {
            if (!win || win.disposed || !win.controlled || win._takingBack) return;
            const key = controlKey(win.hostId, win.sid);
            const host = hostById(win.hostId);
            if (!host) return;
            win._takingBack = true;
            if (win.ctlBtn) win.ctlBtn.disabled = true;
            let r = null, j = null;
            try {
                r = await hostFetch(host, '/session/control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id: win.sid, op: 'release',
                                           gen: win.controlled.gen,
                                           clientId: CLIENT_ID }),
                });
                j = await r.json().catch(() => null);
            } catch (_) {
                r = null;
            } finally {
                win._takingBack = false;
                if (!win.disposed && win.controlled && win.ctlBtn) win.ctlBtn.disabled = false;
            }
            const err = j && j.error;
            if (r && j && ((r.status === 200 && j.ok)
                           || (r.status === 409 && err === 'stale_gen'))) {
                applyControlView(key, j, 'post');
                return;
            }
            if (r && r.status === 409 && err === 'not_active') {
                showLeaseAffordance(win.hostId);
                return;
            }
            if (r && r.status === 404) {
                const w = windows.get(key);
                if (w && !w.disposed) {
                    w.staleSession = true;
                    showNotice('session ' + w.sid + ' no longer exists');
                    closeWindow(key);
                }
                return;
            }
            showNotice('take back failed' + (err ? ': ' + err : ''), { type: 'error' });
        }

        // The lease affordance that already exists for that broker: the HOME
        // Become active overlay (armed, so the answer to its button takes it
        // down again over this still-built view), or a REMOTE broker's mask
        // and click-to-take-over chip.
        function showLeaseAffordance(hostId) {
            if (hostId === homeHostId()) {
                _leaseAffordance.armed = true;
                _leaseAffordance.asked = false;
                showBecomeActiveOverlay();
                return;
            }
            applyRemoteLease(hostId, false);
        }

        // openWindow's tail (67): a terminal that is controlled right now is
        // built greyed and is never attached (the tail's attach is refused).
        // Not a change, so no controlStateChanged: the chip already says so.
        function seedControlledWindow(win) {
            const rec = terminalControl[controlKey(win.hostId, win.sid)];
            if (!rec || rec.state !== 'controlled') return;
            enterControlled(win, Object.freeze({ label: rec.label,
                                                 since: rec.since, gen: rec.gen }));
        }

        // The controlled view of a key: the live window's own state first, the
        // record only for a chip with no open window.
        function controlledViewFor(key) {
            key = String(key);
            const win = windows.get(key);
            if (win && !win.disposed && win.type !== 'app') return win.controlled || null;
            const rec = terminalControl[key];
            return (rec && rec.state === 'controlled') ? rec : null;
        }

        // The one post-apply hook: runs once per applied change, after enter
        // (socket already closed) or leave (reattachWindow already called; the
        // dial is in flight), and for chip-only keys too.
        function controlStateChanged(key) {
            try { updateTaskbarActive(); } catch (_) {}
            try { updateTaskbarLabel(String(key)); } catch (_) {}
        }
