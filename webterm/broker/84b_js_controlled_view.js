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
        // A free poll row carries no gen; it is recorded as gen null, which
        // means "no baseline": the next view of any gen is accepted.
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
