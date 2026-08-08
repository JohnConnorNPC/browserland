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
        // Per-gate policy words/helpers land here next; today this file
        // carries only the restart-reason words.

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
