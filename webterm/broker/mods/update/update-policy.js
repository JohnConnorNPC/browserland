        // ---- update mod: policy words / helpers (#182 Part 2,
        // atom A4) -----------------------------------------------------
        // Split out of update.js the same way update-apply.js was (#182
        // Part 2, A29/A30): a companion script with NO registerMod call,
        // spliced immediately before update-apply.js in ui.py's _MODS list
        // (update-apply.js is itself spliced immediately before update.js,
        // so the combined load order is policy -> apply -> update.js).
        // Every shipped mod script lands in ONE shared inline <script>, so
        // a top-level declaration in an earlier fragment is a name any
        // later fragment's closures can read -- update.js's references to
        // RESTART_REASONS and restartReasonWords resolve here exactly as
        // if they were still declared inside it.
        //
        // Nothing here may reference anything from update.js's own closure
        // (ctx, checkStateFor, updHost, LOCAL_HOST_ID, hostFetch, ...) --
        // that is exactly why these symbols, and only these, could move:
        // each is self-contained. They stay pure and DOM-free for the same
        // reason they were pure in update.js: test_update_fleet.py's node
        // harness executes them.
        //
        // Beside the restart-reason words, this file carries the pure
        // half of writing POST /update/policy (#182 Part 2, atom A5):
        // which keys a broker's view accepts, what one consent click
        // grants, and how a write's answer reads. update.js's
        // setPolicy/offerConsent own the fetch, the op map and the
        // repaint.

        // Human words for every reason_code the broker can hand
        // back, over both routes that carry one: GET /info's
        // `restart.reason_code` (why the control is DISABLED) and
        // POST /restart's own `reason_code` on a non-202 (why an
        // attempted restart was REFUSED, or failed after it was
        // accepted). The two sets overlap almost entirely — a
        // refusal is usually the same gate the button was already
        // disabled for, caught again server-side in case the two
        // ever disagree — so one table answers both. An unrecognised
        // code falls through to a generic sentence in
        // restartReasonWords() below, never a raw token rendered at
        // the user — same posture as update.js's REASONS table, for
        // the same reason.
        const RESTART_REASONS = {
            // Qualified rather than a flat claim (#182 Part 2, atom
            // A7): this table is static/factless -- it cannot see the
            // gate's CURRENT source the way applyGateWords can -- so
            // it names both paths a human could use rather than
            // asserting the config file is the only one.
            'restart-disabled': 'restarting is switched off on this '
                + 'broker — the "Allow this broker to update itself" '
                + 'row switches it on when that row is writable; '
                + 'otherwise the broker’s config decides',
            'no-supervisor': 'this broker was started without the '
                + 'launcher that can bring it back, so nothing '
                + 'would relaunch it — restart it manually on the '
                + 'machine itself',
            'supervisor-ppid-mismatch': 'the process that started '
                + 'this broker is no longer its parent, so the '
                + 'launcher can no longer be trusted to relaunch it',
            'systemd-restart-disabled': 'this broker runs under a '
                + 'systemd unit whose restart policy will not bring '
                + 'it back — stopping it now would leave nothing '
                + 'listening',
            'systemd-policy-unreadable': 'this broker could not '
                + 'read its own systemd unit’s restart policy, so '
                + 'it cannot promise a restart would be honoured',
            'probe-failed': 'this broker could not determine '
                + 'whether anything would bring it back, so it '
                + 'refuses to guess',
            'restart-in-progress': 'a restart is already under way',
            'cooldown': 'this broker came back up moments ago, so '
                + 'another restart is held off for a short cooldown '
                + 'that clears by itself — try again shortly',
            'cross-origin-forbidden': 'this page is not allowed to '
                + 'ask this broker to restart',
            'restart-error': 'the restart machinery itself failed '
                + '— this broker was not touched',
            // The three below come back only from a POST /restart
            // that got past the gate and then failed to complete —
            // never from /info, so they can never be why the button
            // was disabled, only why a click did not work.
            'critical_sections_timed_out': 'writes already in '
                + 'progress on this broker (an upload, a recording '
                + 'save) did not finish in time, so the restart was '
                + 'abandoned rather than risk losing them. Try '
                + 'again shortly',
            'not_supervised': 'this broker discovered only at the '
                + 'last moment that nothing would relaunch it, so '
                + 'the restart was abandoned before anything '
                + 'stopped',
            'drain_failed': 'this broker could not confirm its '
                + 'in-flight writes were safe to leave, so the '
                + 'restart was abandoned',
        };
        // Never a raw token. A code this mod does not recognise —
        // including one of the "drain_error: <exception text>"
        // strings the broker only ever LOGS rather than documents
        // as UI-facing — reads as this rather than as itself.
        function restartReasonWords(code, retryAfterS) {
            const words = RESTART_REASONS[code]
                || 'this broker did not say why';
            // The cooldown is the ONE reason with an honest number
            // attached (/info and the 409 both carry retry_after_s);
            // any other code — or a value that is not a positive
            // number — renders the sentence alone.
            if (code === 'cooldown'
                    && typeof retryAfterS === 'number'
                    && isFinite(retryAfterS) && retryAfterS > 0) {
                return words + ' (about '
                    + Math.ceil(retryAfterS) + 's left)';
            }
            return words;
        }

        // ---- per-gate policy writes (#182 Part 2, atom A5) -------------
        // The broker's update view carries a three-key `policy` block
        // beside its five flat fields (app.py update_policy_view):
        // check/apply/restart, each {enabled, source, mutable}, where
        // `mutable` per gate means what the flat one always did — a
        // present config key owns the gate, so no write can move it.

        // The keys POST /update/policy can be asked to change, in the
        // one fixed order the broker walks them in.
        const POLICY_WRITE_KEYS = ['check_enabled', 'apply_enabled',
                                   'restart_enabled'];
        // The broker_config.json key that owns each gate — what a 409's
        // `locked` list is really naming, so the words can point at the
        // operator's file instead of at a dead switch. Only ever called
        // on a key already validated against POLICY_WRITE_KEYS.
        function policyConfigKeyName(key) {
            if (key === 'apply_enabled') return 'update_apply_enabled';
            if (key === 'restart_enabled') return 'restart_enabled';
            return 'update_check_enabled';
        }

        // Which policy keys a broker's update view says a write could
        // even name. A `policy` block is proof of the three-gate build;
        // a flat view with a real `mutable` bool is the single-key
        // build that shipped first, which accepts only check_enabled;
        // and anything else — no update key at all, or a placeholder
        // that never published `mutable` — predates ANY policy write.
        // This is the old-peer degradation seam the self-update row
        // (A6) builds on: ABSENCE of `policy` on a peer reads as a
        // pre-this-build broker, the same pattern remote_writable's
        // absence already follows.
        function policyKeysFor(upd) {
            if (!upd || typeof upd !== 'object') return [];
            if (upd.policy && typeof upd.policy === 'object') {
                return POLICY_WRITE_KEYS.slice();
            }
            if (typeof upd.mutable === 'boolean') return ['check_enabled'];
            return [];
        }

        // What ONE consent click grants: every gate that is ours to
        // move and not already open, in the fixed check/apply/restart
        // order. `mutable && !enabled` on purpose — a stored "off" that
        // a sidecar write merely synthesized for the check gate (nobody
        // ever clicked off) is GRANTABLE, not a standing revoke, so it
        // reads exactly like a default here. Values are only ever
        // `true`: a revoke is a deliberate per-gate act and nothing may
        // build one from a consent click. Empty object when nothing is
        // grantable — the caller must then not spend a request.
        function consentBody(pol) {
            const body = {};
            if (!pol || typeof pol !== 'object') return body;
            const gates = [['check', 'check_enabled'],
                           ['apply', 'apply_enabled'],
                           ['restart', 'restart_enabled']];
            for (const pair of gates) {
                const g = pol[pair[0]];
                if (g && typeof g === 'object' && g.mutable === true
                        && !g.enabled) {
                    body[pair[1]] = true;
                }
            }
            return body;
        }

        // One interpretation of POST /update/policy's answer, shared by
        // every writer — the checking switch's single-key write and the
        // multi-grant consent alike. `{ ok: true }` on success, else
        // `{ ok: false, phase, note }` where phase/note are exactly
        // what the policy row renders; the sentences for the single-key
        // check cases are byte-for-byte what setChecking always said.
        // Transport failures and the id-reuse fingerprint check stay
        // with the caller: they are about the request and the row, not
        // about the broker's answer.
        function policyWriteOutcome(status, body, wantKeys) {
            const code = body && body.error;
            if (status === 403 && code === 'forbidden_origin') {
                // NOT a credentials problem, and emphatically not a
                // policy one: that broker runs the build that shipped
                // this route origin-gated, so it accepts the change
                // only from its own page. Naming it as anything else
                // sends someone to re-enter a password that is
                // perfectly good.
                return { ok: false, phase: 'failed',
                         note: 'that broker’s build only accepts this '
                             + 'change from its own desktop — update it, '
                             + 'and this switch will work from here' };
            }
            if (status === 401 || status === 403) {
                return { ok: false, phase: 'failed',
                         note: 'that broker refused our password' };
            }
            if (status === 404) {
                // The route does not exist there — the peer predates
                // ANY policy write. Said as a FACT about the build
                // rather than as a failure, naming the one key that
                // does work on it. (A peer that has the route but not
                // the `policy` block is recognised UPSTREAM, via
                // policyKeysFor, before a request is ever built.)
                return { ok: false, phase: 'failed',
                         note: 'that broker predates the switch — an '
                             + 'operator sets "update_check_enabled" in '
                             + 'its config and restarts it' };
            }
            if (status === 409 && code === 'policy_locked') {
                // Which file-owned keys blocked the write. The broker
                // names the config-owned requested keys in `locked`
                // (fixed order); walked as untrusted input, falling
                // back to the keys this write asked for, and to the
                // check alone — today's exact single-key sentence —
                // when neither names one.
                const sent = Array.isArray(body && body.locked)
                    ? body.locked : [];
                let known = sent.filter(function (k) {
                    return POLICY_WRITE_KEYS.indexOf(k) !== -1; });
                if (!known.length) {
                    known = (wantKeys || []).filter(function (k) {
                        return POLICY_WRITE_KEYS.indexOf(k) !== -1; });
                }
                if (!known.length) known = ['check_enabled'];
                const names = POLICY_WRITE_KEYS.filter(function (k) {
                    return known.indexOf(k) !== -1;
                }).map(function (k) {
                    return '"' + policyConfigKeyName(k) + '"';
                });
                const named = names.length > 1
                    ? names.slice(0, -1).join(', ') + ' and '
                        + names[names.length - 1]
                    : names[0];
                return { ok: false, phase: 'locked',
                         note: 'that broker’s config names ' + named
                             + ', so that file decides and this switch '
                             + 'does not' };
            }
            if (!body || body.ok !== true) {
                return { ok: false, phase: 'failed',
                         note: 'that broker refused the change' };
            }
            return { ok: true };
        }

        // ---- fleet words moved out of update.js (#182 Part 2, A6) ------
        // answered/stateWords/WORST_FIRST/aggregate/chipLabel: the pure
        // half of the chip/window state model, moved here for the same
        // 2500-line-cap reason the apply helpers moved to
        // update-apply.js. Same rules as everything above: args-only,
        // DOM-free, nothing from update.js's own closure — which is
        // exactly why these five could move while hostRows/renderChip
        // (which read the closure's host list and records) could not.

        // Did this host actually TELL us something? Only these three
        // come from a version check that ran to completion. Every
        // aggregate below keys "may I say up to date" on this and on
        // nothing else, so a state added later is un-answered until
        // somebody deliberately says otherwise.
        function answered(ps) {
            return ps === 'current' || ps === 'behind'
                || ps === 'ahead-or-diverged';
        }
        // A host's state in words — short enough for one tooltip line
        // or one window row, and distinct for every state, so two hosts
        // in different states can never read the same. The long-form
        // sentence lives in REASONS and rides the window's why-notes.
        function stateWords(ps, st) {
            const chk = st && st.check;
            if (ps === 'current') return 'up to date';
            if (ps === 'behind') {
                const n = chk && chk.behindBy;
                return (typeof n === 'number' && n > 0)
                    ? (n + ' commit' + (n === 1 ? '' : 's') + ' behind')
                    : 'behind upstream';
            }
            if (ps === 'ahead-or-diverged') {
                const a = (chk && chk.aheadBy) || 0;
                const b = (chk && chk.behindBy) || 0;
                return 'ahead by ' + a + ' commit' + (a === 1 ? '' : 's')
                    + (b ? (', behind by ' + b) : '');
            }
            if (ps === 'pending') return 'checking…';
            if (ps === 'route-absent') return 'too old to check';
            if (ps === 'not-opted-in') return 'checking not enabled there';
            if (ps === 'unauthorized') return 'password refused';
            if (ps === 'unreachable') return 'did not answer';
            if (ps === 'unreachable-or-too-old') {
                return 'no answer — asleep, or too old to check';
            }
            return 'could not be checked';
        }
        // Worst first (#149's badge rule: a fault is never hidden behind
        // a healthy majority). 'behind' leads because it is the one
        // state with something to DO about it; the peer failures follow;
        // 'current' is LAST, which is what makes the chip's colour
        // honest — green is reachable only when every other entry in
        // this list is absent from the fleet.
        const WORST_FIRST = ['behind', 'unauthorized', 'unreachable',
                             'unreachable-or-too-old', 'route-absent',
                             'not-opted-in', 'unknown',
                             'pending', 'ahead-or-diverged', 'current'];
        // The fleet in one line. Mirrors the host aggregate badge in 75
        // (renderAggregateChip): the WORST state colours it, but the
        // TEXT counts every abnormal host, so one broker that is behind
        // cannot mask two that never answered.
        //
        // The honesty rule lives in `allCurrent`, and that is why this
        // returns a flag instead of leaving callers to pattern-match the
        // text: it is true only when the parts list came out EMPTY —
        // every configured host answered, and every answer was
        // 'current'. A host that is pending, ahead, unreachable, too
        // old, or not opted in each puts a part on that list and thereby
        // takes the phrase "up to date" off the chip.
        function aggregate(rows) {
            const worst = WORST_FIRST.find(function (s) {
                return rows.some(function (r) { return r.ps === s; });
            }) || 'unknown';
            const count = function (fn) { return rows.filter(fn).length; };
            const behind = count(function (r) {
                return r.ps === 'behind'; });
            const ahead = count(function (r) {
                return r.ps === 'ahead-or-diverged'; });
            const silent = count(function (r) {
                return !answered(r.ps); });
            const parts = [];
            if (behind) parts.push(behind + ' behind');
            if (ahead) parts.push(ahead + ' ahead');
            if (silent) parts.push(silent + ' unchecked');
            // rows is never empty in practice — getHosts() always
            // unshifts the local broker — but an empty list must not
            // fall out of the counts below as "everything is current".
            // Vacuously true is still the one sentence this mod may
            // never print without having checked something.
            if (!rows.length) parts.push('none configured');
            // Every host on its own line, hidden ones included. #178's
            // rule, and it applies doubly to a version: hiding a broker
            // parks it on the desktop, it does not make that broker's
            // build any less stale, so the badge may stop CLAIMING a
            // fault but must never stop REPORTING one.
            const lines = rows.map(function (r) {
                return r.label + ' — ' + r.words
                    + (r.hidden ? ' — hidden' : '');
            });
            return {
                worst: worst,
                allCurrent: parts.length === 0,
                text: rows.length
                    + (rows.length === 1 ? ' broker · ' : ' brokers · ')
                    + (parts.length ? parts.join(', ') : 'up to date'),
                lines: lines,
            };
        }
        function chipLabel(st, s) {
            if (s === 'behind') {
                const n = st && st.check && st.check.behindBy;
                return (typeof n === 'number' && n > 0)
                    ? (n + ' behind') : 'update';
            }
            if (s === 'current') return 'up to date';
            if (s === 'ahead-or-diverged') return 'ahead';
            return 'version ?';
        }

        // ---- the self-update row (#182 Part 2, atom A6) ----------------
        // "Allow this broker to update itself": ONE switch over the two
        // gates that together let a broker pull new code and restart
        // onto it — apply_enabled and restart_enabled, never
        // check_enabled (checking has its own row). Everything wordy or
        // decidable from arguments lives here; update.js keeps only the
        // thin closure wiring (selfUpdateRowNeeded/selfUpdateModelFor/
        // commitRemoteSelfUpdate/renderSelfUpdateRow/
        // confirmRemoteSelfUpdate).

        // The whole second row in one pure verdict: is self-updating ON,
        // is the switch live, what would one click POST, and what the
        // inline note beside it says. `pol` is the update view's
        // `policy` block; `op` is this row's own in-flight/last write
        // ({phase, note, want}) or null.
        //
        // TRANSITION FEASIBILITY, not raw mutability: the row is ON
        // when apply AND restart are both granted, and the OFF row is
        // live only when every gate still false is actually ours to
        // move — a config-owned TRUE gate never blocks enabling the
        // other one, but a config-owned FALSE gate kills the whole row
        // and is NAMED, because a switch that could only half-grant
        // would misdescribe every click. Partial states are surfaced,
        // never hidden: a standing grant behind an aggregate that reads
        // OFF is said out loud.
        //
        // Fail closed on shape: `enabled`/`mutable` must be real
        // booleans on both gates, and a `policy` block that is present
        // but unreadable is reported source-neutrally — NEVER read via
        // the flat fields, which describe the check gate. `pol == null`
        // is the flat single-key build (policyKeysFor said
        // ['check_enabled']): a dead row that says what fixes it.
        function selfUpdateRowModel(pol, op) {
            const m = { on: false, disabled: true,
                        labelWords: 'Allow this broker to update itself',
                        note: '', postKeys: [] };
            const gate = function (g) {
                return (g && typeof g === 'object'
                        && typeof g.enabled === 'boolean'
                        && typeof g.mutable === 'boolean') ? g : null;
            };
            const named = function (pairs) {
                const names = pairs.map(function (p) {
                    return '"' + policyConfigKeyName(p[1]) + '"'; });
                return 'its config names '
                    + (names.length > 1 ? names.join(' and ') : names[0])
                    + ', so that file decides';
            };
            if (pol == null) {
                m.note = 'predates self-update grants — update that '
                    + 'broker';
            } else {
                const a = (typeof pol === 'object')
                    ? gate(pol.apply) : null;
                const r = (typeof pol === 'object')
                    ? gate(pol.restart) : null;
                if (!a || !r) {
                    m.note = 'this broker did not report its '
                        + 'self-update grants in a shape this page '
                        + 'can read';
                } else {
                    const pairs = [[a, 'apply_enabled'],
                                   [r, 'restart_enabled']];
                    m.on = a.enabled && r.enabled;
                    if (m.on) {
                        m.labelWords = 'Stop self-updates';
                        for (const p of pairs) {
                            if (p[0].mutable === true) {
                                m.postKeys.push(p[1]);
                            }
                        }
                        m.disabled = !m.postKeys.length;
                        m.note = m.disabled
                            ? named(pairs.filter(function (p) {
                                  return p[0].mutable !== true; }))
                            : 'this broker may pull new code and '
                                + 'restart itself; Stop withdraws that';
                    } else {
                        const blocked = pairs.filter(function (p) {
                            return !p[0].enabled
                                && p[0].mutable !== true; });
                        const parts = [];
                        if (blocked.length) {
                            parts.push(named(blocked));
                        } else {
                            m.disabled = false;
                            for (const p of pairs) {
                                if (!p[0].enabled) {
                                    m.postKeys.push(p[1]);
                                }
                            }
                        }
                        // A standing grant is surfaced, never hidden
                        // behind an aggregate that reads OFF.
                        for (const p of pairs) {
                            if (p[0].enabled) {
                                parts.push((p[1] === 'apply_enabled'
                                    ? 'applying updates'
                                    : 'restarting itself')
                                    + ' is still granted on it');
                            }
                        }
                        m.note = parts.join(' — ');
                    }
                }
            }
            if (op && typeof op === 'object') {
                if (op.note) m.note = op.note;
                if (op.phase === 'busy') {
                    m.disabled = true;
                    m.labelWords = op.want ? 'Allowing…' : 'Stopping…';
                }
            }
            return m;
        }

        // One click's POST body, from the model's postKeys and the
        // direction the human just asked for. The value is only ever
        // the passed `want` — never a hardcoded false (the
        // no-auto-revoke guard) and never a key outside the three the
        // route accepts.
        function policyChangesFor(keys, want) {
            const changes = {};
            for (const k of (Array.isArray(keys) ? keys : [])) {
                if (POLICY_WRITE_KEYS.indexOf(k) !== -1) {
                    changes[k] = want === true;
                }
            }
            return changes;
        }

        // The 'self' write's busy words, handed to setPolicy via
        // opts.busyNote — its own defaults are check-worded.
        function selfUpdateBusyNote(want) {
            return want ? 'allowing self-updates…'
                : 'stopping self-updates…';
        }

        // Every word of the remote enable confirmation (A6). Names the
        // MACHINE — label and url both, since the label is user-entered
        // and the url is what actually decides where the request goes —
        // and says plainly what is being allowed: that machine will
        // download and run code from GitHub and restart itself.
        function selfUpdateConfirmWords(label, url) {
            const lines = [];
            if (label) lines.push(String(label));
            if (url) lines.push(String(url));
            return {
                title: 'Let that broker update itself?',
                intro: 'This allows another machine — not the one '
                    + 'serving this page — to download and run code '
                    + 'from GitHub and to restart itself.',
                lines: lines,
                warning: 'When its Update button is used, that machine '
                    + 'will pull new code from GitHub and restart '
                    + 'itself. Stop withdraws the permission but '
                    + 'undoes nothing already applied.',
                okLabel: 'Allow self-updates',
            };
        }

        // Which `source` the checking row's dead-button words judge:
        // the per-gate check facts when the `policy` block exists, the
        // flat field for an old peer that predates it (A6). The words
        // themselves are unchanged either way.
        function policyCheckSource(upd) {
            if (!upd || typeof upd !== 'object') return null;
            const pol = upd.policy;
            if (pol && typeof pol === 'object' && pol.check
                    && typeof pol.check === 'object') {
                return pol.check.source;
            }
            return upd.source;
        }
