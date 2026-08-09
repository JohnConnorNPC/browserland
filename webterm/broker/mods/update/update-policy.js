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
            'restart-disabled': 'restarting is switched off on this '
                + 'broker. An operator turns it on in the broker '
                + 'config',
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
