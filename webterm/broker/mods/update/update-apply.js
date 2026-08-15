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

        // The second row's own sentence for the disabled-apply gate
        // (#182 Part 2, atom A6/A7): shown INSTEAD of the config-file
        // sentence above whenever the gate's CURRENT source is one the
        // row can actually move. Built fresh by applyGateWords below on
        // every call -- never cached -- so a gate that moves from
        // stored/default to config-owned (or back) reads correctly on
        // the very next render, with no stale sentence surviving it.
        const APPLY_GATE_ROW_WORDS = 'applying updates is switched '
            + 'off on this broker — "Allow this broker to update '
            + 'itself" below switches it on';

        // `applyPolicy` is the apply gate's CURRENT {enabled, source,
        // mutable} facts (update.js's renderApplyRow reads them off the
        // host's live update view at render time) -- optional, because
        // an older view predates the three-gate `policy` block
        // entirely. Only the disabled-here code branches on it; every
        // other code's words are exactly what they always were.
        // source 'stored'/'default'/'corrupt' names a gate the row can
        // move, so the row sentence names the row. source 'config', or
        // no facts at all (an old view, or a call that predates A6),
        // fails toward the historically-true config-file sentence --
        // the one thing this code can still say for certain when it
        // cannot see the gate's real source.
        //
        // `brokerWords` (A4) is optional and defaults to 'this broker',
        // which keeps every local sentence byte-identical; a remote
        // row passes 'that broker' and gets the same facts with ITS
        // broker named. The quoted "Allow this broker to update
        // itself" is the consent row's LITERAL LABEL -- one per host
        // in this same window -- so it stays verbatim either way; only
        // the unquoted prose swaps.
        // The one deixis rewriter (A4 remediation): RESTART_REASONS'
        // sentences say 'this broker' because #183 wrote them for the
        // one local control, and a remote row splices them in whole.
        // The quoted "Allow this broker to update itself" is a LITERAL
        // control label and must survive verbatim, so the swap works
        // around it rather than through it. bw 'this broker' (or
        // absent) is a byte-identical no-op.
        const APPLY_CONSENT_ROW_LABEL =
            'Allow this broker to update itself';
        function swapDeixis(words, bw) {
            if (typeof words !== 'string'
                    || typeof bw !== 'string' || !bw
                    || bw === 'this broker') {
                return words;
            }
            return words.split(APPLY_CONSENT_ROW_LABEL)
                .map(function (part) {
                    return part.split('this broker').join(bw);
                })
                .join(APPLY_CONSENT_ROW_LABEL);
        }

        function applyGateWords(code, restartWords, applyPolicy,
                                brokerWords) {
            const bw = (typeof brokerWords === 'string' && brokerWords)
                ? brokerWords : 'this broker';
            const remote = bw !== 'this broker';
            if (code === APPLY_GATE_RESTART) {
                return 'applying needs a restart to take effect, and '
                    + restartWords;
            }
            if (remote && code === APPLY_GATE_UNKNOWN_STATE) {
                // The local sentence says 'here', which on a remote
                // row would point at the wrong machine.
                return 'no update check has established a target '
                    + 'commit on ' + bw + ' yet -- run a check first';
            }
            if (code === APPLY_GATE_DISABLED) {
                const source = (applyPolicy
                    && typeof applyPolicy === 'object')
                    ? applyPolicy.source : null;
                if (source === 'stored' || source === 'default'
                        || source === 'corrupt') {
                    return remote
                        ? ('applying updates is switched off on ' + bw
                            + ' — its own "Allow this broker to update '
                            + 'itself" row above switches it on')
                        : APPLY_GATE_ROW_WORDS;
                }
                return remote
                    ? ('applying updates is switched off on ' + bw
                        + '; an operator sets "update_apply_enabled" '
                        + 'in its config and restarts it -- a '
                        + 'config-file decision')
                    : APPLY_GATE_WORDS[code];
            }
            if (remote && code === APPLY_GATE_NOT_BEHIND) {
                return bw + ' is not behind upstream -- there is '
                    + 'nothing to apply';
            }
            return APPLY_GATE_WORDS[code] || null;
        }

        // Every shape POST /update/apply answers with -- every 409
        // refusal message rendered, never a guess at success.
        // status===null is a transport failure; 202/ok:true returns
        // null (waitForApplyBootId owns that, not this function).
        // `brokerWords` (A4) defaults to 'this broker' -- byte-identical
        // local sentences -- and makeApplyFlow passes its own
        // brokerName(hostId) through, so a remote target's refusals
        // name the broker they describe.
        function applyRefusalOutcome(status, body, brokerWords) {
            const bw = (typeof brokerWords === 'string' && brokerWords)
                ? brokerWords : 'this broker';
            if (status === null) {
                return { kind: 'transport', lines: ['could not '
                    + 'reach ' + bw + ' to ask for the update.'] };
            }
            if (status === 202 && body && body.ok === true) return null;
            const err = (body && typeof body === 'object')
                ? body.error : null;
            if (status === 503 && err === 'update_apply_disabled') {
                // Source-neutral on purpose: a refusal carries no
                // fresh facts about WHICH source disabled the gate,
                // so it names both paths rather than claiming one --
                // the row below when the gate is writable, the
                // config file when an operator has pinned it.
                return { kind: 'gate', lines: ['applying updates '
                    + 'is switched off on ' + bw + ' — the "Allow '
                    + 'this broker to update itself" row switches it '
                    + 'on when it is writable; a config key naming '
                    + 'it overrides the row.'] };
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
                return { kind: 'in-progress', lines: [bw
                    + ' already has an update or restart under way'
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
            // A4: the first build to ship POST /update/apply
            // origin-gated it, so it answers a cross-origin apply
            // with 403 forbidden_origin even though its own page can
            // apply fine. NOT a credentials problem and NOT an
            // unknown shape -- the fix is on that machine, once.
            // `err` is only trusted as the exact string: a body with
            // no error key, or a non-string one, falls through to
            // the honest unknown below rather than earning this.
            if (status === 403 && err === 'forbidden_origin') {
                return { kind: 'forbidden-origin', lines: [bw
                    + '’s build still refuses applies driven from '
                    + 'another broker’s page — update it by hand '
                    + 'this once; its newer build will accept '
                    + 'them.'] };
            }
            // Anything else (a wrong password, a malformed request,
            // an unrecognised shape): never a guess at success.
            return { kind: 'unknown', lines: ['the broker '
                + 'answered, but not in a shape this page '
                + 'recognises -- it must not be read as a '
                + 'success.'] };
        }

        // ---- the remote rows' Update button (#182 Part 2, atom A4) --
        // One PURE decision per remote row, over that broker's OWN
        // facts and nobody else's -- its check, its update view, its
        // restart block, its op -- so N rows can disagree honestly and
        // the fleet harness can execute the whole ladder.
        //
        // f = { behind      the row's ps === 'behind'
        //       op          applyFlow.opFor(id) (or the confirm-time
        //                   refusal note), or null
        //       upd         updateCapFor(id) -- that broker's update
        //                   view, or null
        //       coarseState state(st) for that broker's record
        //       check       st.check, that broker's own check payload
        //       restart     restartInfoFor(id) -- its restart block,
        //                   walked untrusted }
        //
        // show: behind OR an op -- the OR is what keeps a busy/settled
        // op's notes on screen while the host flips unreachable
        // mid-restart. live: STRICT flat apply_enabled === true,
        // exactly like the local row -- never a fallback chain from
        // policy.apply.enabled, which would let a legacy flat true
        // resurrect an explicit policy false. Every dead button
        // carries words; the two missing-capability shapes get
        // DIFFERENT sentences, because a missing `policy` block proves
        // an old build while a missing remote_applyable can also be a
        // stale or partial record -- claiming age there would be a
        // guess dressed as a diagnosis.
        function remoteApplyRowModel(f) {
            const op = (f && f.op) || null;
            const busy = !!(op && op.phase === 'waiting');
            const behind = !!(f && f.behind);
            const upd = (f && f.upd && typeof f.upd === 'object')
                ? f.upd : null;
            const target = applyTargetSha(f && f.check);
            // livePath is the MANUAL-NOTE fact (A5), wider than live:
            // a row mid-apply ('waiting') or freshly proven restarted
            // ('done', stale-behind until its recheck lands) is USING
            // its one-click path, so the by-hand note must not name
            // it. A settled failure is not a path -- unless the facts
            // still offer the button, which the final branch adds.
            const out = { show: !!op || behind, busy: busy,
                          live: false, gate: null, targetSha: target,
                          livePath: !!(op && (op.phase === 'waiting'
                              || op.phase === 'done')),
                          words: null };
            if (!out.show) return out;
            if (!upd) {
                // No update view at all: nothing to say -- unless an
                // op is still reporting, whose notes ARE the row.
                out.show = !!op;
                return out;
            }
            if (!('policy' in upd)) {
                out.words = 'predates remote updates — update that '
                    + 'broker';
                return out;
            }
            if (upd.remote_applyable !== true) {
                out.words = 'that broker has not reported support '
                    + 'for applies driven from another broker’s page '
                    + '— update that broker on its own machine';
                return out;
            }
            const rst = (f && f.restart
                && typeof f.restart === 'object')
                ? f.restart : { known: false, available: false };
            out.gate = applyGateFromFacts(
                (f && f.coarseState) || 'unknown', target,
                upd.apply_enabled === true, !!rst.available);
            if (out.gate !== null) {
                // The restart words come from #183's local-control
                // table, so their unquoted 'this broker' prose is
                // rewritten to name THIS row's broker -- the quoted
                // consent-row label survives verbatim (swapDeixis).
                const rw = rst.known === true
                    ? swapDeixis(restartReasonWords(rst.reason,
                        rst.retryAfterS), 'that broker')
                    : 'that broker has not reported a restart '
                        + 'capability yet';
                const ap = (upd.policy
                    && typeof upd.policy === 'object')
                    ? upd.policy.apply : null;
                out.words = applyGateWords(out.gate, rw, ap,
                    'that broker');
                return out;
            }
            out.live = !busy;
            out.livePath = true;
            return out;
        }

        // The confirm dialog's decidable half (A4): title/wording
        // naming the broker, the same range/count guards the local
        // dialog has, the compare url linkified ONLY when it is a
        // plain http(s) url (it is broker-controlled input), and the
        // session cost as what THAT broker reports -- or an explicit
        // unknown when its restart block is absent/malformed, never
        // the serving broker's numbers and never silence.
        function remoteApplyConfirmModel(label, check, restart) {
            const name = (typeof label === 'string' && label)
                ? label : 'that broker';
            const rst = (restart && typeof restart === 'object')
                ? restart : { known: false };
            const known = rst.known === true;
            const cu = check && check.upstream && check.upstream.url;
            return {
                title: 'Apply this update to ' + name + '?',
                restarts: name,
                oldSha: (check && check.local && check.local.sha)
                    || null,
                behindBy: (check
                    && typeof check.behindBy === 'number')
                    ? check.behindBy : null,
                compareUrl: (typeof cu === 'string'
                    && /^https?:\/\//i.test(cu)) ? cu : null,
                continuity: (known && rst.continuity
                    && typeof rst.continuity === 'object')
                    ? rst.continuity
                    : { guaranteed: 0, at_risk: 0, unknown: 0 },
                restart: {
                    ref: 'that broker',
                    // 'not the broker serving this page' is knowable
                    // from here; 'another machine' is not -- two
                    // brokers legitimately share a box.
                    intro: 'This updates and restarts ' + name
                        + ' — not the broker serving this page.'
                        + (known ? (' The session counts below are '
                            + 'what that broker reports about '
                            + 'itself.') : ''),
                    unknown: known ? null
                        : ('Session impact unknown — that broker '
                            + 'has not reported what a restart costs '
                            + 'its live sessions.'),
                },
            };
        }

        // ---- the manual-pull how-to note (atom A5) ------------------
        // Before A4, every behind row needed the exact same by-hand
        // instructions, so one blanket note under the row list was
        // accurate for the whole fleet. A4 gave a live remote row its
        // own Update button, and the local broker already has its own
        // apply row above -- so a behind row can now have a live path,
        // and the old note started rendering beside rows it had
        // nothing left to say to. It renders now only for the brokers
        // that are behind and have NONE, and it names them -- the same
        // way the per-broker reason notes above it do ("r.label + ' — '
        // + reason"): among N rows an unattributed sentence belongs to
        // nobody.
        //
        // verdicts: [{ label, behind, live }, ...] -- update.js's own
        // per-row assembly. `live` is never re-decided here: it is the
        // SAME fact each row's own control already read -- the local
        // apply row's applyGateFromFacts result for the local broker,
        // remoteApplyRowModel's own verdict for every other one -- so
        // this note can never disagree with a button sitting right
        // beside it. Rows that are not behind are never named: an
        // unreachable row already carries its own reason note above,
        // and a current one needs nothing said about it here.
        function manualPullNote(verdicts) {
            const list = Array.isArray(verdicts) ? verdicts : [];
            const names = list.filter(function (v) {
                return v && v.behind && !v.live;
            }).map(function (v) {
                return (v && typeof v.label === 'string' && v.label)
                    || 'an unnamed broker';
            });
            if (!names.length) return null;
            const named = names.length > 1
                ? names.slice(0, -1).join(', ') + ' and '
                    + names[names.length - 1]
                : names[0];
            const each = names.length > 1 ? 'each one' : 'it';
            const whose = names.length > 1 ? 'that broker' : named;
            return { cls: 'app-upd-howto', text: 'To update ' + named
                + ': stop ' + each + ', then on ' + whose + '’s own '
                + 'machine run "git pull --ff-only" in its checkout, '
                + 'reinstall dependencies if pyproject.toml changed, '
                + 'then start it again and reload this page.' };
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

        // ---- the apply flow itself (#182 Part 2, atom A3) ------------------
        //
        // POST /update/apply plus the boot-id wait that proves the restart
        // behind it, HOST-PARAMETERIZED: everything update.js's init(ctx)
        // closure used to hard-pin to the LOCAL broker is handed in through
        // one `deps` object built once by init, so the same machinery can
        // drive an apply against any EXPLICITLY named host -- and so
        // test_update_fleet.py's node harness can drive it with every
        // dependency stubbed. Not pure like the helpers above (it holds the
        // per-host op state and awaits fetches), but DOM-free and
        // closure-free for the same reason they are: nothing here may reach
        // for update.js's own names.
        //
        // deps, one object, built once:
        //   localHostId       the id whose wording says 'this broker'
        //   updHost(id)       id -> live host object, resolved FRESH -- the
        //                     broker being asked is expected to disappear
        //                     and come back mid-wait
        //   hostFingerprint(id)  which MACHINE the id points at right now
        //   hostFetch(host, path, opts)
        //   renderAll()
        //   modCatalogCache   the shared /info capability cache (Map)
        //   recheckHost(id)   re-check that host after a proven restart
        //   isDead()          the mod's unload flag -- one flag, read per
        //                     iteration, stops EVERY host's op at once
        //   sleep(ms) / now() / waitTimeoutMs / pollMs
        //
        // Op state is a Map keyed by host id inside the factory: N hosts
        // can be busy independently, the busy-guard is per host, and a
        // finished op only lands its verdict while it is still the op its
        // Map entry holds -- a stale continuation never clobbers a newer
        // op for the same host.
        function makeApplyFlow(deps) {
            const applyOps = new Map();
            // What a row renders from: { phase, note } or null. note is
            // always an array of lines, exactly as the local-only flow
            // kept it.
            function opFor(hostId) {
                return applyOps.get(hostId) || null;
            }
            // A confirm-time refusal ("that broker changed while the
            // confirmation was open") lands in the SAME per-host op
            // surface the flow itself reports through, so it can never
            // be shadowed by an earlier settled op -- and never
            // shadows a LIVE one: while an apply is 'waiting', that
            // op is the truth and the note is dropped.
            function noteRefusal(hostId, lines) {
                const cur = applyOps.get(hostId);
                if (cur && cur.phase === 'waiting') return false;
                applyOps.set(hostId,
                    { phase: 'failed', note: lines });
                deps.renderAll();
                return true;
            }
            function brokerName(hostId) {
                return hostId === deps.localHostId
                    ? 'this broker' : 'that broker';
            }
            function noHostWords(hostId) {
                return hostId === deps.localHostId
                    ? 'the local broker is no longer configured'
                    : 'that broker is no longer configured';
            }
            // Every phase write after the op is installed goes through
            // here so it can refuse to land on an entry that no longer
            // holds THIS op.
            function settle(hostId, op, phase, note) {
                if (applyOps.get(hostId) !== op) return false;
                op.phase = phase;
                op.note = note;
                deps.renderAll();
                return true;
            }

            // Polls that host's /info until the boot id differs from
            // `beforeBootId`. A transport failure here is THE expected
            // shape of this loop, not a fault -- the broker being asked is
            // mid-stop or mid-relaunch and simply not there to answer --
            // so a rejected fetch, a !ok status and an unreadable body all
            // just continue until the bounded deadline runs out. `fp` pins
            // the MACHINE: null skips the check (the #183 restart flow,
            // which predates it, keeps its exact old behavior); anything
            // else is compared against a fresh hostFingerprint every
            // iteration, because a host id is reusable and an id
            // re-pointed mid-wait must end the wait rather than let a
            // different machine's boot id answer for the one that was
            // asked. isDead is a parameter, not a dep: the restart flow
            // shares this loop under its own dead flag.
            async function pollBootId(hostId, beforeBootId, fp, isDead) {
                const deadline = deps.now() + deps.waitTimeoutMs;
                while (deps.now() < deadline) {
                    await deps.sleep(deps.pollMs);
                    if (isDead()) return 'dead';
                    const host = deps.updHost(hostId);
                    if (!host) return 'no-host';
                    if (fp !== null
                            && deps.hostFingerprint(hostId) !== fp) {
                        return 'host-moved';
                    }
                    let r;
                    try {
                        r = await deps.hostFetch(host, '/info',
                            { cache: 'no-store', timeoutMs: 4000 });
                    } catch (_) { continue; }
                    if (!r.ok) continue;
                    let j = null;
                    try { j = await r.json(); } catch (_) { continue; }
                    const bootId = j && j.restart
                        && typeof j.restart.bootId === 'string'
                        && j.restart.bootId;
                    if (!bootId) continue;
                    if (bootId !== beforeBootId) return 'changed';
                }
                return 'timeout';
            }

            async function waitForApplyBootId(hostId, op, beforeBootId,
                                              fp) {
                const bn = brokerName(hostId);
                settle(hostId, op, 'waiting',
                    ['applying — ' + bn + ' is restarting itself…']);
                const res = await pollBootId(hostId, beforeBootId, fp,
                    deps.isDead);
                if (deps.isDead() || res === 'dead') return;
                if (res === 'no-host') {
                    settle(hostId, op, 'failed', [noHostWords(hostId)]);
                } else if (res === 'host-moved') {
                    // Indeterminate on purpose: the update was sent to the
                    // machine the fingerprint named, and whatever machine
                    // answers under this id now is a different one. Never
                    // a success, and never a poll of the new machine under
                    // the old op.
                    settle(hostId, op, 'failed', ['this host entry now '
                        + 'points at a different machine than the one '
                        + 'the update was sent to — what happened to '
                        + 'that update cannot be told from here.']);
                } else if (res === 'changed') {
                    // A restart is PROVEN, not that it hit the target --
                    // deployStrip reads that off the recheck below.
                    if (settle(hostId, op, 'done', [bn + ' restarted — '
                            + 'checking what build it came up on…'])) {
                        deps.modCatalogCache.delete(hostId);
                        deps.renderAll();
                        await deps.recheckHost(hostId);
                        // Locally the deploy strip completes this
                        // story; a remote row has no strip, so the
                        // note itself must stop claiming a check is
                        // still running once it has finished. The
                        // settle guard drops this if a newer op took
                        // the row meanwhile.
                        if (hostId !== deps.localHostId) {
                            settle(hostId, op, 'done', [bn
                                + ' restarted and was re-checked — '
                                + 'its row shows the build it came '
                                + 'back on.']);
                        }
                    }
                    return;
                } else {
                    settle(hostId, op, 'timeout', [bn + ' did not come '
                        + 'back within '
                        + Math.round(deps.waitTimeoutMs / 1000)
                        + 's — this window cannot tell whether it is '
                        + 'still starting.']);
                }
            }

            // The ONLY caller of POST /update/apply -- no timer, no poll.
            // Takes a host ID, never a host object, and REQUIRES one: a
            // null/undefined/absent id returns before any fetch, because a
            // null host reaching hostFetch would silently aim the apply at
            // the SERVING origin -- the wrong-machine bug this signature
            // exists to prevent (the same rule setPolicy follows).
            async function performApply(hostId, targetSha) {
                if (typeof hostId !== 'string' || !hostId) return;
                const cur = applyOps.get(hostId);
                // Per-host busy-guard: host X mid-apply never blocks --
                // and never reads as busy for -- host Y.
                if (cur && cur.phase === 'waiting') return;
                const host = deps.updHost(hostId);
                if (!host) {
                    applyOps.set(hostId, { phase: 'failed',
                        note: [noHostWords(hostId)] });
                    deps.renderAll();
                    return;
                }
                // Which MACHINE the id names right now, captured with the
                // POST and re-checked on every poll iteration after it.
                const fp = deps.hostFingerprint(hostId);
                const op = { phase: 'waiting',
                    note: ['sending the update request…'] };
                applyOps.set(hostId, op);
                deps.renderAll();
                let resp = null;
                try {
                    resp = await deps.hostFetch(host, '/update/apply', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ target_sha: targetSha }),
                        timeoutMs: 30000,
                    });
                } catch (e) {
                    // A rejected POST is NOT the expected mid-restart
                    // connection death -- that grace applies only AFTER a
                    // 202, once the broker has committed to stopping.
                    settle(hostId, op, 'failed',
                        applyRefusalOutcome(null, null,
                            brokerName(hostId)).lines);
                    return;
                }
                let body = null;
                try { body = await resp.json(); } catch (_) {}
                if (resp.status === 202 && body && body.ok === true) {
                    if (typeof body.bootId === 'string' && body.bootId) {
                        // The 202's bootId IS the poll baseline.
                        await waitForApplyBootId(hostId, op, body.bootId,
                            fp);
                        return;
                    }
                    // Accepted -- the broker may already be merging or
                    // restarting -- but with nothing to confirm it by.
                    // NOT a refusal (it was accepted) and never assumed
                    // to have failed: a naive retry here could start a
                    // SECOND apply on top of one already under way.
                    settle(hostId, op, 'failed', ['the broker accepted '
                        + 'the update but did not report a boot id to '
                        + 'confirm it by -- it may already be applying; '
                        + 'check before trying again']);
                    return;
                }
                const outcome = applyRefusalOutcome(resp.status, body,
                    brokerName(hostId));
                const lines = outcome ? outcome.lines.slice()
                    : ['the broker answered, but not in a shape this '
                        + 'page recognises.'];
                if (outcome && outcome.kind === 'incomplete'
                        && outcome.reasonCode) {
                    lines.push('Why the restart could not be armed: '
                        + restartReasonWords(outcome.reasonCode));
                }
                settle(hostId, op, 'failed', lines);
            }

            return { performApply: performApply,
                     pollBootId: pollBootId,
                     opFor: opFor,
                     noteRefusal: noteRefusal };
        }
