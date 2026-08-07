        // ---- update mod: apply / post-apply pure helpers (#182 Part 2,
        // A29 + atom A30) -----------------------------------------------
        // Split out of update.js the same way editor.js/codemirror.js are
        // (#146): a companion script with NO registerMod call, spliced
        // immediately before update.js in ui.py's _MODS list. Every shipped
        // mod script lands in ONE shared inline <script>, so a top-level
        // declaration in an earlier fragment is a name any later
        // fragment's closures can read -- update.js's init(ctx) calls the
        // functions below exactly as if they were still declared inside it.
        //
        // Nothing here may reference anything from update.js's own closure
        // (ctx, checkStateFor, updHost, LOCAL_HOST_ID, hostFetch, ...) --
        // that is exactly why these functions, and only these, could move:
        // each takes every fact it needs as a plain argument. They stay
        // pure and DOM-free for the same reason they were pure in
        // update.js: test_update_fleet.py's node harness executes them.

        // ---- update APPLY: can it run from here? (#182 Part 2,
        // atom A30). Pure gate/parser -- a COURTESY only; the
        // server re-proves everything, and every refusal it can
        // send is rendered in full by applyRefusalOutcome below.
        const APPLY_GATE_DISABLED = 'apply-disabled-here';
        const APPLY_GATE_RESTART = 'restart-unavailable-here';
        const APPLY_GATE_UNKNOWN_STATE = 'unknown-state';
        const APPLY_GATE_NOT_BEHIND = 'not-behind';
        const APPLY_GATE_NO_TARGET = 'no-target-sha';
        const APPLY_GATE_WORDS = {
            'apply-disabled-here': 'applying updates is switched '
                + 'off on this broker; an operator sets '
                + '"update_apply_enabled" in its config and '
                + 'restarts it -- a config-file decision',
            'unknown-state': 'no update check has established a '
                + 'target commit here yet -- run a check first',
            'not-behind': 'this broker is not behind upstream -- '
                + 'there is nothing to apply',
            'no-target-sha': 'the check named no exact commit for '
                + 'this apply to target',
        };

        // Release mode names a tag, never a sha -- absent then.
        function applyTargetSha(check) {
            const sha = check && check.upstream && check.upstream.sha;
            return (typeof sha === 'string'
                && /^[0-9a-f]{40}$/.test(sha)) ? sha : null;
        }

        // ASSUMPTION: priority among unmet conditions is not fixed
        // by the brief; operator-level gates go first here.
        function applyGateFromFacts(coarseState, targetSha,
                                    applyEnabled, restartAvailable) {
            if (!applyEnabled) return APPLY_GATE_DISABLED;
            if (!restartAvailable) return APPLY_GATE_RESTART;
            if (coarseState === 'unknown') return APPLY_GATE_UNKNOWN_STATE;
            if (coarseState !== 'behind') return APPLY_GATE_NOT_BEHIND;
            if (!targetSha) return APPLY_GATE_NO_TARGET;
            return null;
        }

        function applyGateWords(code, restartWords) {
            return code === APPLY_GATE_RESTART
                ? ('applying needs a restart to take effect, and '
                    + restartWords)
                : (APPLY_GATE_WORDS[code] || null);
        }

        // Every shape POST /update/apply answers with -- every 409
        // refusal message rendered, never a guess at success.
        // status===null is a transport failure; 202/ok:true returns
        // null (waitForApplyBootId owns that, not this function).
        function applyRefusalOutcome(status, body) {
            if (status === null) {
                return { kind: 'transport', lines: ['could not '
                    + 'reach this broker to ask for the update.'] };
            }
            if (status === 202 && body && body.ok === true) return null;
            const err = (body && typeof body === 'object')
                ? body.error : null;
            if (status === 503 && err === 'update_apply_disabled') {
                return { kind: 'gate', lines: ['applying updates '
                    + 'is switched off on this broker; an operator '
                    + 'sets "update_apply_enabled" in its config '
                    + 'and restarts it.'] };
            }
            if (status === 503 && err === 'apply_incomplete') {
                const rc = (body
                    && typeof body.reason_code === 'string')
                    ? body.reason_code : null;
                return { kind: 'incomplete', reasonCode: rc,
                    lines: ['The files on disk were moved to the '
                        + 'build this apply named, but this broker '
                        + 'never restarted onto them -- it is '
                        + 'STILL RUNNING THE OLD CODE. A manual '
                        + 'restart will pick up the new tree.'] };
            }
            if (status === 409
                    && (err === 'apply_in_progress'
                        || err === 'restart_in_progress')) {
                const opId = (body
                    && typeof body.operation_id === 'string')
                    ? (' (operation ' + body.operation_id + ')')
                    : '';
                return { kind: 'in-progress', lines: ['this broker '
                    + 'already has an update or restart under way'
                    + opId + ' -- try again once it finishes.'] };
            }
            if (status === 409
                    && (err === 'apply_refused'
                        || err === 'apply_failed')) {
                const refusals = Array.isArray(body.refusals)
                    ? body.refusals : [];
                const lines = refusals
                    .filter((r) => r && typeof r === 'object')
                    .map((r) => (typeof r.message === 'string'
                        && r.message) ? r.message
                        : ('refused: ' + (r.reason || '?')));
                if (!lines.length) {
                    lines.push('the broker refused the update but '
                        + 'did not say why.');
                }
                if (err === 'apply_failed' && body.tree_suspect) {
                    lines.push('The merge failed partway through '
                        + '-- this checkout may need a human on '
                        + 'the machine itself.');
                }
                return { kind: (err === 'apply_refused')
                    ? 'refused' : 'failed', lines: lines };
            }
            // Anything else (a wrong password, a malformed request,
            // an unrecognised shape): never a guess at success.
            return { kind: 'unknown', lines: ['the broker '
                + 'answered, but not in a shape this page '
                + 'recognises -- it must not be read as a '
                + 'success.'] };
        }

        // ---- after an apply (#182 Part 2, A29) ---------------------
        // A broker that has been through an apply-restart reports the
        // finalized outcome as `last_deploy` beside its check payload,
        // because the process that could have said it live is the one
        // the restart replaced. Everything in this block derives what
        // the window renders from that object plus the live session
        // list — pure, no DOM, so test_update_fleet.py can execute it.
        //
        // Scoped to the LOCAL broker, deliberately: sessions on any
        // host can be stale, but an apply happens on the machine whose
        // page you are looking at, and a peer's deploy history belongs
        // on that peer's own desktop, not on a row of this fleet list.

        // The outcome object, walked as UNTRUSTED input the way
        // servesUpdateMod walks a catalog: the broker only promises a
        // string `outcome`; every other field is taken only when it
        // has the advertised type, and anything else reads as absent.
        // null = no deploy history, and no deploy history renders
        // NOTHING — an empty strip would be a claim about an apply
        // that never happened.
        function deployOutcome(ld) {
            if (!ld || typeof ld !== 'object') return null;
            if (typeof ld.outcome !== 'string' || !ld.outcome) {
                return null;
            }
            const rec = (ld.record && typeof ld.record === 'object')
                ? ld.record : {};
            const str = function (v) {
                return (typeof v === 'string' && v) ? v : null;
            };
            return {
                outcome: ld.outcome,
                detail: str(ld.detail),
                observedSha: str(ld.observedSha),
                oldSha: str(rec.oldSha),
                targetSha: str(rec.targetSha),
            };
        }
        function shortSha(sha) {
            return sha ? String(sha).slice(0, 10) : null;
        }

        // #22's stale rule, computed the way the broker itself
        // computes it (app.py: `version != broker_version`): an AGENT
        // session whose reported build differs from this broker's own
        // — a pre-#22 agent reporting none included — is still running
        // code this broker no longer runs. Agents only: a plain
        // terminal legitimately reports no version (flagging it would
        // be noise), and a plain terminal cannot survive a restart
        // anyway. Returns null — never 0 — when either side is
        // unreadable, because "could not count" rendered as "none are
        // stale" would be this mod's one forbidden sentence wearing a
        // different hat.
        function staleSurvivors(brokerVersion, sessionList) {
            if (typeof brokerVersion !== 'string' || !brokerVersion) {
                return null;
            }
            if (!Array.isArray(sessionList)) return null;
            let n = 0;
            for (const s of sessionList) {
                if (!s || typeof s !== 'object') continue;
                if (s.kind !== 'agent') continue;
                if (String(s.version || '') !== brokerVersion) n += 1;
            }
            return n;
        }

        // What the post-apply strip says — or null, which renders
        // nothing at all. One branch per outcome the supervisor or
        // the worker's own cancel path can finalize, plus a refusal
        // to read anything unrecognised as success. `cls` bands the
        // strip the way bandFor bands a row (ok / warn / bad), and
        // `newTerminal` is the ONLY affordance: there is no relaunch
        // and no replay, because after a restart the broker's
        // in-memory registry is gone and "bring my session back on
        // the new code" is a promise this code cannot keep.
        // Surviving sessions are left running, counted, and named
        // for what they are.
        function deployStrip(st, sessionList) {
            const d = deployOutcome(st && st.lastDeploy);
            if (!d) return null;
            const chk = st && st.check;
            const bv = (chk && chk.local
                && typeof chk.local.version === 'string'
                && chk.local.version) ? chk.local.version : null;
            const out = { outcome: d.outcome,
                          cls: 'app-upd-deploy-bad',
                          lines: [], survivors: null,
                          newTerminal: false };
            const why = function (label) {
                if (d.detail) out.lines.push(label + d.detail);
            };
            if (d.outcome === 'came-up-ready-on-target') {
                out.cls = 'app-upd-deploy-ok';
                out.newTerminal = true;
                out.survivors = staleSurvivors(bv, sessionList);
                out.lines.push('This broker was updated and came '
                    + 'back on the build the apply named'
                    + (shortSha(d.observedSha)
                        ? (' (' + shortSha(d.observedSha) + ')') : '')
                    + '.');
                if (out.survivors === null) {
                    out.lines.push('Whether any surviving session '
                        + 'is still on the previous build could not '
                        + 'be determined from here.');
                } else if (out.survivors > 0) {
                    out.lines.push(out.survivors
                        + ' surviving agent session'
                        + (out.survivors === 1 ? ' is' : 's are')
                        + ' still running the previous build — a '
                        + 'restart never reloads a session’s code, '
                        + 'so each keeps its old build until it is '
                        + 'relaunched by hand.');
                } else {
                    out.lines.push('No surviving agent session is '
                        + 'still running a previous build.');
                }
            } else if (d.outcome === 'rolled-back') {
                out.lines.push('The last update failed to start and '
                    + 'was rolled back'
                    + (shortSha(d.oldSha)
                        ? (' to ' + shortSha(d.oldSha)) : '')
                    + ' — this broker is running the build from '
                    + 'before that update.');
                why('What failed: ');
            } else if (d.outcome === 'rollback-failed') {
                out.lines.push('The last update failed to start AND '
                    + 'rolling back to the previous build also '
                    + 'failed — this checkout may need a human on '
                    + 'the machine itself.');
                why('What failed: ');
            } else if (d.outcome === 'rollback-impossible') {
                out.lines.push('The last update failed to start and '
                    + 'could not be rolled back — this checkout may '
                    + 'need a human on the machine itself.');
                why('What failed: ');
            } else if (d.outcome === 'came-up-on-wrong-sha') {
                out.cls = 'app-upd-deploy-warn';
                out.lines.push('This broker came back up, but on '
                    + (shortSha(d.observedSha)
                        || 'a commit it could not read')
                    + (shortSha(d.targetSha)
                        ? (' rather than the '
                            + shortSha(d.targetSha)
                            + ' the apply named') : '')
                    + ' — alive, but not the build that was asked '
                    + 'for.');
                why('What it reported: ');
            } else if (d.outcome === 'cancelled-before-restart') {
                out.cls = 'app-upd-deploy-warn';
                out.lines.push('The last apply stopped before its '
                    + 'restart, so this broker never stopped '
                    + 'running the build it was on.');
                if (d.observedSha && d.oldSha) {
                    out.lines.push(d.observedSha !== d.oldSha
                        ? ('The files on disk were already moved '
                            + 'to ' + shortSha(d.observedSha)
                            + ' — newer than the code this broker '
                            + 'is running.')
                        : 'The files on disk were not changed.');
                }
                why('Why it stopped: ');
            } else {
                // A verdict this build does not know. NEVER read as
                // success: an unrecognised outcome reported
                // optimistically is the same lie as an unchecked
                // "up to date".
                out.lines.push('The last update reported an outcome '
                    + 'this page does not recognise — it must not '
                    + 'be read as a success.');
                why('What it reported: ');
            }
            return out;
        }

        // ---- forced-refresh refusals (Check now) ---------------------------
        //
        // The broker floors and budgets forced refreshes because they bypass
        // the daily TTL, and the budget they spend is GitHub's 60/hour for the
        // WHOLE source IP. When it refuses it still answers 200 with the
        // answer it already had -- so these words exist to stop that reading
        // as "just checked". Pure: reason in, sentence out.
        function refreshRefusedWords(ref) {
            const secs = (ref && typeof ref.retry_after_s === 'number'
                          && ref.retry_after_s > 0) ? ref.retry_after_s : null;
            const soon = secs
                ? (secs >= 90 ? (' -- try again in about '
                                 + Math.round(secs / 60) + ' min')
                              : (' -- try again in about ' + secs + 's'))
                : '';
            const reason = ref && ref.reason;
            if (reason === 'rate-limited') {
                // Upstream's own word, not ours. Clicking harder is what made
                // this, and would extend it.
                return 'GitHub is rate-limiting this broker, so it kept the '
                    + 'answer it had' + soon;
            }
            if (reason === 'too-soon') {
                return 'it re-asked GitHub moments ago, so it kept that '
                    + 'answer' + soon;
            }
            if (reason === 'hourly-budget') {
                return 'it has spent this hour of manual re-asks and kept '
                    + 'the answer it had' + soon;
            }
            return 'it did not re-ask GitHub, so this is the answer it '
                + 'already had' + soon;
        }
