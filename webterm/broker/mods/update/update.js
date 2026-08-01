        // ---- mod: update (#182) -------------------------------------------
        // Is the build you are looking at current with upstream? Finding that
        // out otherwise means opening a shell, cd-ing to the checkout and
        // running git fetch.
        //
        // Ships DISABLED (defaultEnabled:false) like aistatus, and for the same
        // reason: nothing here runs at top level except registerMod(), because
        // a top-level side effect would defeat the default-off contract. But
        // note the mod's switch is NOT what protects the network — the broker
        // has its own `update_check_enabled` gate, and answers 503 until an
        // operator sets it. Enabling this mod on a broker that never opted in
        // gets you an honest "checking is disabled here", not a silent egress.
        //
        // The single rule this mod exists to keep: NEVER claim "up to date"
        // when the truth is "I could not check". Every failure lands on
        // `unknown` carrying a reason, and the window shows the reason. A
        // version checker that guesses is worse than no version checker,
        // because it is trusted.
        registerMod({
            id: 'update',
            version: '1.0.0',
            ctxVersion: 1,
            defaultEnabled: false,   // #182: ship OFF — no egress until opted in
            tiers: ['taskbar', 'settings', 'window'],
            init: function (ctx) {
                // How often the BROWSER asks. The broker caches for a day, so
                // most of these are cache hits that never leave the machine;
                // this interval only governs how quickly a tab notices that the
                // broker's own cache turned over.
                const POLL_MS = 30 * 60 * 1000;

                // Chip colour bands — border + text only, background stays the
                // theme bg, so it reads on any theme exactly like .host-chip.
                const BANDS = {
                    green: { border: '#3a6a4a', fg: 'var(--ok)' },
                    amber: { border: '#a8842c', fg: 'var(--warn)' },
                    grey:  { border: 'var(--bg-3)', fg: 'var(--fg-dim)' },
                };
                // Why we could not answer, in words. The broker sends a stable
                // machine reason; anything unrecognised falls through to a
                // generic line rather than rendering a raw token at the user.
                const REASONS = {
                    'no-git': 'this install carries no git commit, so there is '
                        + 'nothing to compare — a build id alone cannot tell '
                        + 'two different builds of the same version apart',
                    'compare-unavailable': 'GitHub could not compare these '
                        + 'commits. That happens when the commit was never '
                        + 'pushed, was garbage-collected, or the branch was '
                        + 'force-pushed — it does NOT by itself mean you are '
                        + 'ahead',
                    'rate-limited': 'GitHub’s rate limit is exhausted for '
                        + 'this address. The check will resume by itself',
                    'offline': 'could not reach GitHub',
                    'bad-response': 'GitHub’s answer was not in the '
                        + 'expected form',
                    'too-large': 'the comparison was too large to read',
                    // NOT a transport failure and NOT an error: a broker in
                    // this state answered us perfectly, it simply never opted
                    // in. Named 'not-opted-in' rather than 'disabled' on
                    // purpose — 'disabled' reads as a fault to go and fix, and
                    // it is also the word this mod's OWN on/off switch wears,
                    // so one key meaning either would be a key nobody can read.
                    // The distinction that matters most is against 'offline'
                    // and 'unreachable' below: those describe a request that
                    // did not get through, and this one describes an answer.
                    'not-opted-in': 'update checking is switched off on this '
                        + 'broker. That is its operator’s choice, not a fault '
                        + 'here and not a network problem — an operator enables '
                        + 'it with "update_check_enabled" in the broker config',
                    // The five below are raised HERE, in the browser, and
                    // never by a broker. Keeping them separate from 'offline'
                    // is the whole point: 'offline' is the BROKER saying IT
                    // could not reach GitHub. Reusing it for a broker that did
                    // not answer US would announce a GitHub outage every time a
                    // peer is asleep or its url is wrong — a failure one hop
                    // earlier, described as one hop further out.
                    'unreachable': 'this broker did not answer, so its version '
                        + 'could not be read. That is the connection to the '
                        + 'broker itself, not to GitHub',
                    // The genuinely ambiguous one, and it is honest about being
                    // ambiguous. A broker that serves no desktop page reports an
                    // empty mod list whatever routes it has, so if it also
                    // publishes no update capability there is no way from here
                    // to tell "too old to have the check" from "has it, and is
                    // asleep" — a missing route dies in the cross-origin
                    // preflight and comes back looking exactly like a machine
                    // that is down. Naming one of the two would be a guess
                    // dressed as an answer, so this names both.
                    'unreachable-or-too-old': 'this headless broker did not '
                        + 'answer. It is either asleep or running a build from '
                        + 'before update checking existed — from here those two '
                        + 'are indistinguishable, because a missing route fails '
                        + 'in exactly the same way as an unreachable machine. '
                        + 'Check that it is running; if it is, update it',
                    'broker-error': 'this broker answered, but not with a '
                        + 'version check that could be read',
                    'no-such-host': 'the host this answer belonged to is no '
                        + 'longer configured, so there was nobody left to ask',
                    // The last two come from the capability probe below, before
                    // any version check is sent. 'route-absent' is the only
                    // reason in this whole table that describes a request that
                    // was never made: the peer is running a build from before
                    // the check existed, and we know that from its /info rather
                    // than from watching the request fail.
                    'route-absent': 'this broker is running a build from before '
                        + 'update checking existed, so it has no version check '
                        + 'to ask for. It is deliberately not asked — the '
                        + 'request would die in that broker’s cross-origin '
                        + 'preflight and arrive back here as an unexplained '
                        + 'network error, which reads exactly like a machine '
                        + 'that is asleep. Update that broker and this answers '
                        + 'itself',
                    'unauthorized': 'this broker refused our password, so '
                        + 'nothing about it could be read — set its password on '
                        + 'the Browser tab in Settings',
                };

                // ---- live state (NOT persisted — it is a live check) ----
                // One record PER HOST, keyed by host id, following the core's
                // pollStateFor() precedent. A single set of module scalars
                // could only ever describe one broker, and "this one is
                // current, that one is 3 behind" is the normal case for anyone
                // running more than one — not an edge case. So the answer is
                // stored beside the id it came from, and two hosts can hold
                // different states at the same time.
                //
                // Record fields:
                //   hostId     the id it was fetched FOR, so a record can never
                //              be read out from under the host it describes
                //   check      the broker's check payload (null = nothing good)
                //   error      string reason when the request itself failed
                //   checkedAt  epoch ms of the last COMPLETED poll (0 = never
                //              answered, which renders as "checking…", not as
                //              a stale timestamp)
                //   inFlight   this host's own mutex. Deliberately per host:
                //              one global one would let a single black-holed
                //              broker sit on the 20s timeout and suppress
                //              every other host's poll for the whole window.
                const hostChecks = new Map();   // hostId -> check state record
                function checkStateFor(hostId) {
                    let st = hostChecks.get(hostId);
                    if (!st) {
                        st = { hostId: hostId, check: null, error: null,
                               checkedAt: 0, inFlight: false };
                        hostChecks.set(hostId, st);
                    }
                    return st;
                }
                // ---- broker addressing (#161 doctrine) ----
                // Callers carry a host ID, never a host OBJECT, and the object
                // is resolved from that id at the moment of the call. A host
                // object captured once goes stale: prefs adopting a /state
                // revision REBUILDS the host array, so a kept reference can
                // hold a url or token that has since been re-entered in
                // Settings, or name a broker that has been removed outright.
                //
                // An unresolved host is not harmless here, which is why nothing
                // in this mod may fall through to a null one. Handed a NULL
                // host, hostFetch builds its URL as '' + path and attaches no
                // Authorization header, so it quietly issues an
                // UNAUTHENTICATED request to the SERVING origin: this broker
                // would answer on a peer's behalf, and this broker's version
                // would then be rendered under the peer's name. That is exactly
                // the lie the mod exists to prevent, so an id that no longer
                // resolves fails closed on 'no-such-host' and never reaches
                // hostFetch at all.
                //
                // The local broker is named by the literal id 'local' rather
                // than by the core's local-host helper: the core synthesises
                // getHosts()[0] as the local record with id 'local', so the
                // literal keeps meaning the same broker even if that list is
                // reordered, and unlike a getHosts()[0] fallback it is
                // greppable — a 'local' in a diff is a decision somebody made
                // on purpose, while a positional fallback is one nobody did.
                const LOCAL_HOST_ID = 'local';
                function updHost(hostId) {
                    return hostById(hostId || LOCAL_HOST_ID);
                }
                let timer = null;
                const openWins = new Set();

                // The issue's own objection to a permanent taskbar chip: this
                // is consulted twice a month, not continuously. So by default
                // the chip only appears when there is something to say.
                const quietSetting = ctx.settings.boolean(
                    'update.hideWhenCurrent', true, {
                        title: 'Update check',
                        label: 'hide the chip while the build is current',
                        isBrowserGlobal: true,
                    });
                quietSetting.onChange(function () { renderChip(); });

                // ---- taskbar chip ----
                const chip = document.createElement('div');
                chip.id = 'update-chip';
                chip.title = 'build version check';
                chip.style.cssText = [
                    'flex:0 0 auto', 'display:inline-flex', 'align-items:center',
                    'gap:5px', 'font-family:monospace', 'font-size:11px',
                    'padding:2px 8px', 'border-radius:3px',
                    'border:1px solid var(--bg-3)', 'background:var(--bg)',
                    'color:var(--fg-dim)', 'user-select:none',
                    'white-space:nowrap', 'cursor:pointer', 'margin-left:2px',
                ].join(';');
                const chipIcon = document.createElement('span');
                chipIcon.className = 'update-chip-ic';
                chipIcon.setAttribute('aria-hidden', 'true');
                chipIcon.innerHTML = appIconSvg('update');
                const chipText = document.createElement('span');
                chipText.textContent = '…';
                chip.appendChild(chipIcon);
                chip.appendChild(chipText);
                chip.addEventListener('click', openOrFocusWindow);
                ctx.taskbar.addStatusItem(chip);   // auto-removed on unload

                // The readers all take the RECORD they are describing. Passing
                // it in (instead of reaching for whatever the module last saw)
                // is what stops a second host's answer being rendered under the
                // first host's name once the poll fans out.
                function state(st) {
                    if (!st) return 'unknown';        // never established
                    if (st.error) return 'unknown';
                    return (st.check && st.check.state) || 'unknown';
                }
                function reasonCode(st) {
                    if (!st) return null;
                    if (st.error) return st.error;
                    return (st.check && st.check.reason) || null;
                }
                function bandFor(s) {
                    if (s === 'current') return 'green';
                    if (s === 'behind') return 'amber';
                    return 'grey';   // every non-answer, and ahead, land here
                }

                // ---- one host, one state --------------------------------
                // The four ways a PEER can leave us without an answer. Each is
                // its own state rather than one lumped 'unknown', because they
                // ask four different things of whoever reads them: update that
                // broker, switch checking on over there, re-enter its password,
                // or go find out why the machine is not answering. Telling an
                // operator to chase a network fault when the truth is "that
                // operator never opted in" is the same class of mistake as
                // claiming 'current' — a confident sentence about the wrong
                // thing. They are exactly the capability probe's non-'ready'
                // outcomes, and every one is a REASONS key, so each has words.
                const PEER_FAILURES = ['route-absent', 'not-opted-in',
                                       'unauthorized', 'unreachable',
                                       'unreachable-or-too-old'];
                // The fine-grained read of a record: what state() returns, plus
                // 'pending' for a host nothing has come back from yet, plus the
                // four above pulled up out of 'unknown'. state() is deliberately
                // left alone — the single-broker chip still speaks its coarse
                // vocabulary, so none of this can change what one broker reads
                // as. Nothing here can return 'current' for a host that did not
                // answer: 'current' arrives only on st.check.state, which is
                // only ever assigned from a parsed 200 body.
                function peerState(st) {
                    if (!st) return 'unknown';
                    // No poll has COMPLETED for this host. Its own state on
                    // purpose (75's 'pending' precedent): 'unknown' would
                    // announce a failure that has not happened, and 'current'
                    // would be the exact lie this mod exists to prevent.
                    if (!st.checkedAt) return 'pending';
                    if (st.error) {
                        return PEER_FAILURES.indexOf(st.error) !== -1
                            ? st.error : 'unknown';
                    }
                    return (st.check && st.check.state) || 'unknown';
                }
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
                // One snapshot row per CONFIGURED host, in host-list order.
                // Hosts are resolved HERE, at render time, so a broker added or
                // removed in Settings is in or out of the very next paint with
                // no separate invalidation step. The row keeps the id as well as
                // the label so anything that later acts on a row re-resolves
                // from the id rather than trusting the label it painted.
                function hostRows() {
                    return allHosts().map(function (h) {
                        const st = checkStateFor(h.id);
                        const ps = peerState(st);
                        return { id: h.id, label: h.label, hidden: !!h.hidden,
                                 st: st, ps: ps, words: stateWords(ps, st) };
                    });
                }
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

                // ONE chip for the whole fleet. Two brokers cannot have two
                // chips: the taskbar is not a dashboard, and #149 already
                // settled that argument for the host chips — N collapse into
                // one aggregate whose tooltip carries the per-host detail.
                function renderChip() {
                    // Read at paint time: the chip must describe the host list
                    // as it is NOW, not as it was when the mod loaded.
                    const rows = hostRows();
                    // One host takes the presentation this mod shipped with,
                    // verbatim — same label, same tooltip, same coarse state()
                    // vocabulary — so fanning out cannot change what a
                    // single-broker install reads. 75's renderHostStatus
                    // branches on exactly this count for exactly this reason.
                    const one = rows.length === 1;
                    const agg = one ? null : aggregate(rows);
                    const st = one ? rows[0].st : null;
                    const s = one ? state(st) : null;
                    // "Quiet when current" hides only the ALL-current case,
                    // never an unknown: a chip that hid itself while it could
                    // not check would be indistinguishable from a green one,
                    // which is exactly the confusion this mod exists to
                    // prevent. Across N hosts that means every one of them
                    // answered and every one is current — a single unreachable
                    // peer keeps the chip on screen, because the fleet's state
                    // is then not known to be clean.
                    const allCurrent = one ? (s === 'current') : agg.allCurrent;
                    const quiet = quietSetting.get() && allCurrent;
                    chip.style.display = quiet ? 'none' : 'inline-flex';
                    const band = BANDS[bandFor(one ? s : agg.worst)];
                    chip.style.borderColor = band.border;
                    chip.style.color = band.fg;
                    chipText.textContent = one ? chipLabel(st, s) : agg.text;
                    chip.title = one ? chipTitle(st)
                        : (agg.lines.join('\n')
                            + '\n— click for the full report');
                    // Keyboard/AT parity is cheap, and a multi-line title does
                    // not exist on touch at all, so the per-host breakdown rides
                    // aria-label too (75's aggregate badge again). Assigned on
                    // BOTH paths so dropping from N hosts back to one cannot
                    // strand the old fleet text on the element.
                    chip.setAttribute('aria-label', one
                        ? (chipText.textContent + ' — ' + chip.title)
                        : (agg.text + ': ' + agg.lines.join('; ')));
                }

                function chipTitle(st) {
                    const s = state(st);
                    if (s === 'current') return 'this build is current with upstream';
                    if (s === 'behind') return 'a newer build is available';
                    if (s === 'ahead-or-diverged') {
                        return 'this checkout has commits upstream has not seen';
                    }
                    const r = REASONS[reasonCode(st)];
                    return 'could not check: ' + (r || 'reason unavailable');
                }

                // ---- can this broker even be asked? (#182) ----------------
                // GET /update/check is a NEW route. A peer running a build from
                // before it existed has no handler for that path and no entry
                // for it in the explicit OPTIONS preflight list, so a
                // cross-origin request to it dies in the preflight and comes
                // back as an opaque TypeError — the very same TypeError a
                // sleeping machine produces. Asking-and-guessing would therefore
                // collapse "I could not reach it" and "it is too old to have the
                // route" into one indistinguishable failure, and the second of
                // those is not really a failure at all: it is a fixed fact about
                // that broker which we could have KNOWN before spending a
                // request on it.
                //
                // So nothing is asked until the answer is known to exist. The
                // probe rides GET /info — a route every broker back to the
                // beginning answers, and one already in every broker's preflight
                // list. That is exactly why the capability was published there
                // (app.py `_info`, `update: {check_enabled, apply_enabled}`)
                // instead of on a route of its own: an older peer answers /info
                // normally and simply lacks the key, which is a difference a
                // client can SEE, rather than one it has to infer from a
                // network error.
                //
                // It does NOT fetch /info itself. fetchModCatalog() in the
                // control panel already GETs it, already classifies the outcome
                // into five states, and already caches that outcome — failures
                // included. A second fetcher here would be a second opinion
                // about the same peer, and two caches that can disagree about
                // whether a broker is awake is precisely how one pane ends up
                // saying "asleep" while another says "fine". This reads the
                // shared record and derives from it.
                //
                // Caching is not re-implemented for the same reason: that shared
                // record IS the cache. A peer that is asleep is probed once and
                // answered from memory on every tick afterwards, so a 30-minute
                // poll can never turn into a retry loop against a machine that
                // is not there. "Check now" is the deliberate retry (recheck()).
                //
                // Five outcomes. Four of them are failures with words, and each
                // one is a REASONS key above so the window can say it — and,
                // being REASONS keys, they are also the peer STATES the chip and
                // the rows render, which is what keeps them from collapsing into
                // one indistinguishable grey:
                //   'ready'         ask it — the route is there
                //   'not-opted-in'  the route is there and that operator never
                //                   opted in. /info already said so, so the
                //                   request is skipped rather than spent
                //                   earning a 503 we can already predict
                //   'route-absent'  it predates the route. NOT asked.
                //   'unauthorized'  it refused our password
                //   'unreachable'   nothing came back from it at all
                //
                // Layer 1 below reads `rec.update`, the capability /info
                // publishes, which fetchModCatalog copies onto the record it
                // caches (81, `rec.update = ...`). If that copy is ever dropped
                // the probe still fails safe: it falls through to layers 2-4,
                // which never ask a peer that lacks the route — the only loss is
                // the shortcut that answers a `check_enabled:false` broker
                // without a round trip.
                function servesUpdateMod(mods) {
                    // A peer's catalog is UNTRUSTED input, so this walks it the
                    // way mod-sync's planFor does: one null or non-object row is
                    // enough to throw on `m.id` and take the whole probe down
                    // with it, and a probe that throws is a broker reported as
                    // unreachable for a shape we could have stepped over.
                    if (!Array.isArray(mods)) return false;
                    for (const m of mods) {
                        if (m && typeof m === 'object' && m.id === 'update') {
                            return true;
                        }
                    }
                    return false;
                }
                // The layering, most authoritative first. Every branch ends in
                // an outcome; nothing falls through to "ask it and hope".
                function capabilityFrom(rec) {
                    if (!rec) return 'unreachable';
                    // Did this peer ANSWER /info? On 'unauthorized' and
                    // 'unreachable' the record is the fetcher's own default
                    // shape (empty mods, no keys) rather than anything the peer
                    // said, so reading a capability out of one would be reading
                    // our own placeholder back as if it were evidence.
                    const answered = rec.state === 'ok'
                        || rec.state === 'headless'
                        || rec.state === 'unsupported';
                    if (answered) {
                        // (1) The capability itself. Authoritative: the key only
                        // exists on a build that also registers the route, so
                        // its mere presence proves the route is there — and it
                        // is the ONLY source that knows whether that operator
                        // opted in, which is why a check_enabled:false broker is
                        // answered from here instead of over the wire.
                        const upd = rec.update;
                        if (upd && typeof upd === 'object') {
                            return upd.check_enabled === false
                                ? 'not-opted-in' : 'ready';
                        }
                        // (2) No key, but the update MOD is in its served
                        // catalog. The mod and the route ship in the same build,
                        // so the row is proof of the route — and it stays proof
                        // whether or not anybody has the mod switched on, since
                        // /info reports what a broker SERVES, not what some
                        // browser runs.
                        if (servesUpdateMod(rec.mods)) return 'ready';
                        // (3) Headless. `mods` is empty on a broker that serves
                        // no page REGARDLESS of what routes it has (the catalog
                        // is only populated inside app.py's serve_ui block), so
                        // here the empty array is evidence of nothing at all.
                        // Absence of proof is not proof of absence: ask, and let
                        // the answer — or the failure to get one — decide.
                        //
                        // But say WHICH kind of asking this is. A headless peer
                        // that publishes no `update` key is one of two things we
                        // genuinely cannot tell apart from here: old enough to
                        // have no route, or new enough to have one and merely
                        // asleep. If it answers, the ambiguity is gone. If it
                        // does not, the failure looks EXACTLY like the opaque
                        // preflight death of a missing route, and reporting that
                        // as 'did not answer' would send someone hunting a
                        // network fault when the fix is "update that broker".
                        // 'unproven' is ready-that-asks, tagged so poll() can
                        // report the ambiguity instead of resolving it by guess.
                        if (rec.state === 'headless') return 'unproven';
                        // (4) It answered, it serves a UI, it published no
                        // capability and it serves no update mod: it predates
                        // #182. Also where 'unsupported' lands — a broker whose
                        // /info has no `mods` array at all predates #157, and
                        // anything older than #157 is necessarily older than
                        // #182 too. Either way the route is not there, so the
                        // request that would have failed opaquely is never sent.
                        return 'route-absent';
                    }
                    if (rec.state === 'unauthorized') return 'unauthorized';
                    return 'unreachable';
                }
                // Takes the RESOLVED host, not an id: it is called from inside
                // poll(), immediately after poll resolved that host, and both
                // the probe and the check it authorises are deliberately aimed
                // at that one resolved record. Re-resolving in between would
                // open the door to probing one broker and asking another.
                async function capabilityFor(host, refresh) {
                    // Fetch only when nothing is cached for this host, or when
                    // an operator explicitly asked for a re-probe. A cached
                    // FAILURE counts as cached: it is the answer. Re-asking a
                    // broker that did not answer, every tick, for as long as the
                    // tab is open, is the loop this exists to avoid.
                    if (refresh || !modCatalogCache.has(host.id)) {
                        // Deliberately NOT gated on modCatalogFetching. That set
                        // marks an in-flight GET but carries no promise to wait
                        // on, so skipping on it would leave us with no record
                        // and report a live broker as unreachable. A duplicate
                        // /info costs one request; a wrong verdict is the bug
                        // this whole mod is written around.
                        try { await fetchModCatalog(host); } catch (_) {}
                    }
                    return capabilityFrom(modCatalogCache.get(host.id));
                }

                // ---- the poll ----
                // Takes a host ID, never a host object: the object is resolved
                // here, at call time, so an edited url/token is picked up on the
                // next poll instead of the check being aimed at a dead address.
                // Everything it learns lands in THAT host's record and nowhere
                // else.
                //
                // opts.refresh forces the capability probe to re-read /info
                // instead of trusting what it already knows about this broker.
                async function poll(hostId, opts) {
                    const hid = hostId || LOCAL_HOST_ID;
                    const st = checkStateFor(hid);
                    // Resolved HERE, immediately before the request, from the
                    // id — not carried in from the caller and not captured at
                    // init.
                    const host = updHost(hid);
                    if (!host) {
                        // The host was removed between scheduling and firing.
                        // A hostFetch with a null host silently hits the
                        // SERVING origin, which would report this broker's
                        // version under some other host's name — so this
                        // degrades to unknown rather than asking anyone.
                        st.check = null;
                        st.error = 'no-such-host';
                        st.checkedAt = Date.now();
                        renderAll();
                        return;
                    }
                    // Per host, not global: a broker that is hanging on its own
                    // 20s timeout only ever blocks itself.
                    if (st.inFlight) return;
                    st.inFlight = true;
                    // Which failure this is, if the try does not finish.
                    // Starts at 'unreachable' because until hostFetch RESOLVES
                    // nothing has come back from this broker at all, and is
                    // narrowed the moment something does. Deliberately never
                    // 'offline' — that is the broker's own word for "I could
                    // not reach GitHub", and pinning it on a peer that is
                    // merely asleep reports an outage that is not happening.
                    // This covers the CHECK only: the capability probe below
                    // reports its own outcomes and swallows its own transport
                    // failure, so it never lands here.
                    let failure = 'unreachable';
                    try {
                        // Ask nothing until it is established that there is
                        // something there to ask. Every outcome except 'ready'
                        // IS the answer for this tick — it is recorded as the
                        // reason and no version check is sent, which for
                        // 'route-absent' is the entire point: that request would
                        // have failed its preflight and been reported as a
                        // machine that is asleep.
                        const cap = await capabilityFor(
                            host, !!(opts && opts.refresh));
                        // 'unproven' asks like 'ready' does, but a failure to
                        // get an answer means something different for it — see
                        // capabilityFrom layer (3). Pre-loading the reason here
                        // is what keeps that distinction from being lost the
                        // moment the request dies.
                        if (cap === 'unproven') failure = 'unreachable-or-too-old';
                        if (cap !== 'ready' && cap !== 'unproven') {
                            st.check = null;
                            st.error = cap;
                            st.checkedAt = Date.now();
                        } else {
                            const r = await hostFetch(host, '/update/check',
                                                      { timeoutMs: 20000 });
                            // This broker answered. Anything that goes wrong
                            // from here on is its ANSWER being unusable, not the
                            // trip to it, and the two must not be reported as
                            // one.
                            failure = 'broker-error';
                            if (r.status === 401 || r.status === 403) {
                                // It refused our password. That is one of the
                                // named peer states with words already written,
                                // and it is emphatically not "its answer was
                                // unreadable" — there IS no answer, there is a
                                // door with a lock on it.
                                //
                                // Reachable even though the probe classifies
                                // 401s of its own: modCatalogCache has no TTL,
                                // so a token that goes stale AFTER page load
                                // (broker restart with a fresh auth_token, an
                                // operator rotating it) leaves a cached 'ok'
                                // and every later check 401s. Without this the
                                // taskbar host chip would read "password
                                // required" while this window said "could not
                                // be checked" — two surfaces contradicting each
                                // other about one broker.
                                st.check = null;
                                st.error = 'unauthorized';
                            } else if (r.status === 503) {
                                // The broker's own gate. A capability that is
                                // absent here — NOT an error, NOT "up to date",
                                // and emphatically NOT a transport failure: this
                                // broker was reached and it answered. Landing a
                                // 503 on 'offline' or 'unreachable' would report
                                // a machine that is up and healthy as a network
                                // problem, and send whoever read it looking for
                                // a fault that does not exist.
                                //
                                // Still reachable after the probe: an older peer
                                // can have the route while publishing no
                                // `update` key on /info, and then the 503 is the
                                // only authority on the gate. Same reason key
                                // either way, so an operator cannot tell (and
                                // does not need to) which of the two said it.
                                st.check = null;
                                st.error = 'not-opted-in';
                            } else if (!r.ok) {
                                throw new Error('HTTP ' + r.status);
                            } else {
                                const j = await r.json();
                                if (!j || !j.ok || !j.check) {
                                    throw new Error('bad-response');
                                }
                                st.check = j.check;
                                st.error = null;
                            }
                            st.checkedAt = Date.now();
                        }
                    } catch (e) {
                        // Degrade to unknown — never to "current".
                        st.check = null;
                        st.error = failure;
                        st.checkedAt = Date.now();
                    } finally {
                        st.inFlight = false;
                    }
                    renderAll();
                }

                // The driver. EVERY configured host, not just the local one:
                // per-host state is worth nothing if only one host is ever
                // filled in, and "this one is current, that one is three weeks
                // stale" is the normal case for anybody running more than one
                // broker — it is the reason to look at all.
                //
                // Concurrent, never sequential. Each poll settles into its own
                // record and repaints as it lands, so a broker sitting on the
                // full 20 s timeout delays nothing but itself; a serial loop
                // would make every tick cost the sum of the slowest, which is
                // precisely the barrier 75's taskbar tick had to remove. Nothing
                // is awaited across hosts, so there is no shared deadline to
                // miss. Re-entry is already handled per host by st.inFlight, so
                // a tick firing while the previous one is still out re-polls
                // only the hosts that have come back.
                //
                // opts is passed straight through, so "Check now" re-probes the
                // whole fleet on the one code path that polls it.
                function pollTick(opts) {
                    const hosts = allHosts();
                    pruneChecks(hosts);
                    // .catch because nobody awaits this. poll() swallows its own
                    // failures, but an unguarded rejection from anywhere else in
                    // it would surface as an unhandled rejection on the page
                    // rather than as a state in this mod.
                    return Promise.all(hosts.map(function (h) {
                        return poll(h.id, opts);
                    })).catch(function () {});
                }
                // Records for hosts that are no longer configured are DROPPED,
                // never merged forward — the same GC 75 runs over hostPolls
                // (~line 691) after each taskbar tick. Nothing renders them
                // (every reader walks allHosts()), so this is about the map not
                // growing for the life of a long-lived tab as hosts are added
                // and removed.
                //
                // A record deleted while its own poll is in flight is simply
                // orphaned: that poll writes into an object nobody holds any
                // more and its answer is discarded, which is the correct outcome
                // for a broker that has since been removed. checkStateFor()
                // would hand a re-added host a fresh record, so the orphan can
                // never be read back out under the new one's name.
                function pruneChecks(hosts) {
                    const ids = new Set(hosts.map(function (h) {
                        return h.id; }));
                    for (const id of Array.from(hostChecks.keys())) {
                        if (!ids.has(id)) hostChecks.delete(id);
                    }
                }
                // The deliberate retry. The capability probe caches its outcome
                // — including its failures — precisely so a tick can never
                // re-ask a broker that is asleep or too old, which leaves
                // exactly one way for that verdict to ever change: somebody
                // asking for it. "Check now" is that somebody, so it re-reads
                // /info as well as re-asking for the version. Without this, a
                // broker updated while the tab was open would stay 'route-absent'
                // until the page was reloaded.
                //
                // Fleet-wide, because the window it sits in reports the fleet: a
                // button that refreshed one row of a list it did not name would
                // leave the other rows looking equally fresh and quietly stale.
                function recheck() { return pollTick({ refresh: true }); }

                function start() {
                    stop();
                    timer = setInterval(pollTick, POLL_MS);
                }
                function stop() {
                    if (timer) { clearInterval(timer); timer = null; }
                }

                // ---- self-restart (#183) -----------------------------------
                // Deliberately scoped to the LOCAL broker, and only the local
                // broker. The window above lists every configured host, but
                // POST /restart enforces a same-origin check on the far end
                // and touching a REMOTE machine from here is an explicit
                // non-goal — so everything below reads and acts on
                // updHost(LOCAL_HOST_ID) alone, resolved fresh at every step
                // (never captured across an await), and the control says so
                // in its own label rather than leaving the scope to be
                // inferred from whichever row of the fleet list the reader's
                // eye happens to be on.
                //
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
                // the user — same posture as REASONS above, for the same
                // reason.
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
                function restartReasonWords(code) {
                    return RESTART_REASONS[code]
                        || 'this broker did not say why';
                }

                // The local broker's restart capability, read off the SAME
                // cached /info record the update-check probe above reads
                // `rec.update` from — modCatalogCache, populated by the
                // Control Panel's fetchModCatalog and refreshed by this
                // mod's own poll cycle — never a second fetcher run in
                // parallel with it. If that record does not carry a
                // `restart` key at all (an older cache shape, or simply not
                // fetched yet) this is UNAVAILABLE with `known: false`: the
                // absence of the capability is not evidence that it is
                // present, and a control that assumed otherwise would be
                // exactly the blind attempt this feature exists to refuse.
                function restartInfo() {
                    const rec = modCatalogCache.get(LOCAL_HOST_ID);
                    const r = rec && rec.restart;
                    if (!r || typeof r !== 'object') {
                        return { known: false, available: false,
                                 reason: null,
                                 continuity: { guaranteed: 0, at_risk: 0,
                                              unknown: 0 },
                                 bootId: null };
                    }
                    const c = (r.continuity && typeof r.continuity === 'object')
                        ? r.continuity : {};
                    const num = function (v) {
                        return (typeof v === 'number' && v > 0) ? v : 0;
                    };
                    return {
                        known: true,
                        available: !!r.available,
                        reason: r.reason_code || null,
                        continuity: { guaranteed: num(c.guaranteed),
                                     at_risk: num(c.at_risk),
                                     unknown: num(c.unknown) },
                        bootId: (typeof r.bootId === 'string' && r.bootId)
                            || null,
                    };
                }

                // ---- the operation itself -----------------------------
                // ONE restart, tracked at module scope: this always targets
                // the one fixed broker, never a row the reader picked, so
                // there is exactly one outcome to track no matter how many
                // update windows happen to be open. Every phase transition
                // calls renderAll(), the same driver poll() already uses,
                // so every open window (and the taskbar chip, harmlessly)
                // repaints together.
                //   null                        nothing happening
                //   { phase:'waiting', note }    POST sent, or polling
                //                                /info for a changed bootId
                //   { phase:'done', note }       bootId changed — confirmed
                //   { phase:'failed', note }     refused, or the request
                //                                itself never landed
                //   { phase:'timeout', note }    the bounded wait ran out
                let restartOp = null;
                // Flipped by ctx.onUnload so a wait loop already in flight
                // when the mod is switched off stops touching the DOM (and
                // stops polling) rather than running to its own timeout in
                // the background of a mod that is no longer loaded.
                let restartOpDead = false;

                function restartSleep(ms) {
                    return new Promise(function (resolve) {
                        setTimeout(resolve, ms);
                    });
                }

                // How long a click waits for proof before giving up and
                // saying so, and how often it asks while waiting. Generous
                // against RESTART_DRAIN_TIMEOUT (20s server-side) plus
                // whatever the supervisor/systemd takes to relaunch the
                // process and have it start answering /info again — bounded
                // regardless, because "wait forever" is a spinner that is
                // indistinguishable from a broker that is never coming
                // back.
                const RESTART_WAIT_TIMEOUT_MS = 90 * 1000;
                const RESTART_POLL_MS = 2000;

                async function waitForNewBootId(beforeBootId) {
                    restartOp = { phase: 'waiting', note: 'restarting…' };
                    renderAll();
                    const deadline = Date.now() + RESTART_WAIT_TIMEOUT_MS;
                    while (Date.now() < deadline) {
                        await restartSleep(RESTART_POLL_MS);
                        if (restartOpDead) return;
                        // Resolved fresh every iteration, never carried in —
                        // the same discipline poll() applies to every other
                        // host in this mod, and doubly necessary here since
                        // the very broker being asked is expected to
                        // disappear and come back mid-loop.
                        const host = updHost(LOCAL_HOST_ID);
                        if (!host) {
                            restartOp = { phase: 'failed',
                                note: 'the local broker is no longer '
                                    + 'configured' };
                            renderAll();
                            return;
                        }
                        let r;
                        try {
                            r = await hostFetch(host, '/info',
                                { cache: 'no-store', timeoutMs: 4000 });
                        } catch (_) {
                            // THE expected shape of this loop, not a
                            // failure: the broker is mid-stop or
                            // mid-relaunch and simply is not there to
                            // answer. Reporting this as an error would tell
                            // the truth about the symptom and lie about
                            // what it means.
                            continue;
                        }
                        if (!r.ok) continue;
                        let j = null;
                        try { j = await r.json(); } catch (_) { continue; }
                        const bootId = j && j.restart
                            && typeof j.restart.bootId === 'string'
                            && j.restart.bootId;
                        if (!bootId) continue;
                        if (bootId !== beforeBootId) {
                            // THE proof. Not the 202 earlier, and not this
                            // response merely arriving — the identity
                            // changed, which is the one fact an HTTP
                            // response from the process being replaced can
                            // never assert about itself.
                            restartOp = { phase: 'done',
                                note: 'restarted — this build is now live' };
                            // Stale the instant the process changed: the
                            // capability, the version, everything this
                            // cached record described belonged to the
                            // broker that just stopped existing.
                            modCatalogCache.delete(LOCAL_HOST_ID);
                            renderAll();
                            recheck();
                            return;
                        }
                        // Still answering, but still the OLD process (or
                        // briefly the new one before it has settled) — keep
                        // waiting.
                    }
                    if (restartOpDead) return;
                    restartOp = { phase: 'timeout',
                        note: 'this broker did not come back within '
                            + Math.round(RESTART_WAIT_TIMEOUT_MS / 1000)
                            + 's. It may still be starting, or it may need '
                            + 'attention on the machine itself — this '
                            + 'window cannot tell which.' };
                    renderAll();
                }

                async function performRestart() {
                    const host = updHost(LOCAL_HOST_ID);
                    if (!host) {
                        restartOp = { phase: 'failed',
                            note: 'the local broker is no longer '
                                + 'configured' };
                        renderAll();
                        return;
                    }
                    restartOp = { phase: 'waiting',
                        note: 'sending the restart request…' };
                    renderAll();
                    let resp;
                    try {
                        resp = await hostFetch(host, '/restart', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({}),
                            // Comfortably past the server's own 20s drain
                            // ceiling (RESTART_DRAIN_TIMEOUT) plus room for
                            // the stop that follows a 202.
                            timeoutMs: 30000,
                        });
                    } catch (e) {
                        // A rejected POST here is NOT the expected
                        // connection-drops-mid-restart case — that applies
                        // only AFTER a 202, once the server has actually
                        // committed to stopping. A request that never
                        // landed at all is reported plainly.
                        restartOp = { phase: 'failed',
                            note: 'could not reach this broker to ask for '
                                + 'a restart' };
                        renderAll();
                        return;
                    }
                    let body = null;
                    try { body = await resp.json(); } catch (_) {}
                    if (resp.status !== 202) {
                        const code = body && body.reason_code;
                        restartOp = { phase: 'failed',
                            note: 'restart refused: '
                                + restartReasonWords(code) };
                        renderAll();
                        return;
                    }
                    const beforeBootId = body && typeof body.bootId === 'string'
                        && body.bootId;
                    if (!beforeBootId) {
                        // Accepted, but with nothing to compare against —
                        // there is no honest way from here to later claim
                        // this succeeded, so it is reported as a failure to
                        // confirm rather than assumed to have worked.
                        restartOp = { phase: 'failed',
                            note: 'the broker accepted the restart but did '
                                + 'not report a boot id to confirm it by' };
                        renderAll();
                        return;
                    }
                    await waitForNewBootId(beforeBootId);
                }

                // The confirm dialog's body: the live-session cost, stated
                // plainly and never rounded toward safety. `at_risk`
                // sessions are LOST, `unknown` ones MAY be — presenting
                // either as "should be fine" would be the same lie this
                // whole mod exists to refuse, aimed at a button instead of
                // a chip.
                function restartConfirmBody(cont) {
                    return function (c) {
                        const msg = document.createElement('div');
                        msg.className = 'app-dialog-msg';
                        msg.textContent = 'This restarts the broker '
                            + 'serving this page. Any other broker '
                            + 'configured here is never touched.';
                        c.appendChild(msg);
                        const list = document.createElement('div');
                        list.className = 'app-upd-restart-continuity';
                        const line = function (text, cls) {
                            const d = document.createElement('div');
                            d.className = 'app-upd-restart-cline'
                                + (cls ? ' ' + cls : '');
                            d.textContent = text;
                            list.appendChild(d);
                        };
                        const plural = function (n) {
                            return n === 1 ? '' : 's';
                        };
                        if (cont.guaranteed) {
                            line(cont.guaranteed + ' agent session'
                                + plural(cont.guaranteed)
                                + ' should reconnect.');
                        }
                        if (cont.at_risk) {
                            line(cont.at_risk + ' agent session'
                                + plural(cont.at_risk) + ' will be LOST.',
                                'app-upd-restart-bad');
                        }
                        if (cont.unknown) {
                            line(cont.unknown + ' agent session'
                                + plural(cont.unknown) + ' MAY be lost — '
                                + 'their fate could not be determined.',
                                'app-upd-restart-warn');
                        }
                        if (!cont.guaranteed && !cont.at_risk
                                && !cont.unknown) {
                            line('No agent sessions are tracked on this '
                                + 'broker right now.');
                        }
                        line('Every plain terminal on this broker — not '
                            + 'an agent session — does not survive a '
                            + 'restart at all.', 'app-upd-restart-bad');
                        c.appendChild(list);
                    };
                }

                function onRestartClick() {
                    if (restartOp && restartOp.phase === 'waiting') return;
                    const info = restartInfo();
                    if (!info.available) return;  // the button is disabled
                                                   // for this; defensive only
                    openDialog({
                        title: 'Restart this broker?',
                        body: restartConfirmBody(info.continuity),
                        buttons: [
                            { label: 'Restart', value: true, primary: true,
                              danger: true },
                            { label: 'Cancel', value: false },
                        ],
                    }).then(function (res) {
                        if (!res || !res.value) return;
                        return performRestart();
                    }).catch(function () {});
                }

                // One row: the button plus its inline status/reason.
                // Rebuilt on every renderWindow() pass, like every other
                // row in this window — the button's text/disabled state is
                // derived fresh from restartOp + restartInfo() each call
                // rather than held on the element, so a poll tick firing
                // mid-wait cannot leave a stale label behind.
                function renderRestartRow(body) {
                    const info = restartInfo();
                    const busy = !!(restartOp
                        && restartOp.phase === 'waiting');
                    const row = document.createElement('div');
                    row.className = 'app-upd-restart-row';
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'app-upd-restart-btn';
                    btn.textContent = busy ? 'Restarting…'
                        : 'Restart this broker';
                    btn.title = 'restarts the broker serving THIS page '
                        + 'only — never a remote host';
                    btn.disabled = busy || !info.available;
                    btn.addEventListener('mousedown', function (e) {
                        e.stopPropagation();
                    });
                    btn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        onRestartClick();
                    });
                    row.appendChild(btn);
                    const status = document.createElement('span');
                    status.className = 'app-upd-restart-inline';
                    if (restartOp && (busy || restartOp.phase === 'done'
                            || restartOp.phase === 'timeout'
                            || restartOp.phase === 'failed')) {
                        status.textContent = restartOp.note;
                        if (restartOp.phase === 'done') {
                            status.classList.add('app-upd-green');
                        } else if (restartOp.phase === 'timeout'
                                || restartOp.phase === 'failed') {
                            status.classList.add('app-upd-amber');
                        }
                    } else if (!info.available) {
                        status.textContent = info.known
                            ? restartReasonWords(info.reason)
                            : 'this broker has not reported a restart '
                                + 'capability yet';
                        status.classList.add('app-upd-grey');
                    }
                    row.appendChild(status);
                    body.appendChild(row);
                }

                // ---- detail window (ephemeral, like task-manager) ----
                function openUpdateWindow(appData) {
                    const id = String(appData.id);
                    const title = appData.title || 'Update check';
                    const geom = clampGeom(appData.geom
                        || appDefaultGeom('text-editor'));
                    const color = normalizeHex(appData.color || defaultColor(id));
                    const locked = appData.locked !== undefined
                        ? !!appData.locked : true;

                    const chrome = buildAppChrome({
                        id, appClass: 'app-upd', badge: '#upd',
                        geom, color, locked, title,
                    });
                    const { dom, titleText } = chrome;

                    const toolbar = document.createElement('div');
                    toolbar.className = 'app-toolbar app-upd-toolbar';
                    const refreshBtn = document.createElement('button');
                    refreshBtn.type = 'button';
                    refreshBtn.textContent = 'Check now';
                    refreshBtn.title = 're-ask every configured broker '
                        + '(each caches its answer for a day)';
                    toolbar.appendChild(refreshBtn);
                    const checkedEl = document.createElement('span');
                    checkedEl.className = 'app-upd-checked';
                    toolbar.appendChild(checkedEl);

                    const body = document.createElement('div');
                    body.className = 'app-upd-body';

                    dom.appendChild(toolbar);
                    dom.appendChild(body);
                    addResizeHandles(dom);

                    document.getElementById('desktop').appendChild(dom);
                    document.getElementById('desktop').classList.remove('empty');

                    const win = {
                        id, sid: 'upd', hostId: 'app',
                        type: 'app', appKind: 'update',
                        dom, body, titleText, checkedEl,
                        term: null, fitAddon: null,
                        ws: null, wsOpen: false, termReady: false,
                        minimized: false, disposed: false,
                        geom, name: title, color,
                        resizeTimer: null, lastSentDims: null,
                        cleanups: [],
                        tiled: false,
                        floatGeom: appData.floatGeom
                            ? Object.assign({}, appData.floatGeom) : null,
                        locked, dirty: false,
                    };
                    windows.set(id, win);
                    openWins.add(win);
                    win.cleanups.push(function () { openWins.delete(win); });

                    const stopProp = (e) => e.stopPropagation();
                    const wireBtn = (btn, fn) => {
                        const onClick = (e) => { e.stopPropagation(); fn(); };
                        btn.addEventListener('mousedown', stopProp);
                        btn.addEventListener('click', onClick);
                        win.cleanups.push(function () {
                            btn.removeEventListener('mousedown', stopProp);
                            btn.removeEventListener('click', onClick);
                        });
                    };
                    wireBtn(refreshBtn, function () { recheck(); });

                    wireAppChrome(win, chrome);

                    const appSess = { key: id, sid: 'upd', id, title,
                                      stale: false, kind: 'app', hostId: 'app' };
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

                    renderWindow(win);
                    finishWindowPlacement(win);
                    return win;
                }

                // One labelled row. Values go through .textContent — everything
                // here except our own literals came off the network, and a
                // broker label is user-entered text on top of that. rowCls lets
                // the per-broker block widen its own label column without
                // needing a second copy of this function.
                function addRow(body, label, value, cls, rowCls) {
                    const row = document.createElement('div');
                    row.className = 'app-upd-row' + (rowCls ? ' ' + rowCls : '');
                    const k = document.createElement('span');
                    k.className = 'app-upd-key';
                    k.textContent = label;
                    const v = document.createElement('span');
                    v.className = 'app-upd-val' + (cls ? ' ' + cls : '');
                    v.textContent = value;
                    row.appendChild(k);
                    row.appendChild(v);
                    body.appendChild(row);
                    return v;
                }

                function addNote(body, text, cls) {
                    const el = document.createElement('div');
                    el.className = 'app-upd-note' + (cls ? ' ' + cls : '');
                    el.textContent = text;
                    body.appendChild(el);
                    return el;
                }

                // A section divider. The window now has two kinds of row —
                // facts about the comparison (shared by every broker, because
                // the upstream is one constant repository) and facts about one
                // broker — and a reader who cannot tell them apart would read a
                // shared "Upstream" line as belonging to the row above it.
                function addHead(body, text) {
                    const el = document.createElement('div');
                    el.className = 'app-upd-head';
                    el.textContent = text;
                    body.appendChild(el);
                    return el;
                }

                // The toolbar's timestamp. Across several brokers it reports the
                // OLDEST completed check, never the newest: the newest would
                // date the freshest row and silently imply the same freshness
                // for a broker whose answer is an hour older. While ANY host has
                // no completed poll at all it reads 'checking…', because there
                // is no honest single time to print with one still outstanding.
                // With one host that is exactly the behaviour it always had.
                function renderChecked(win, rows) {
                    let oldest = 0;
                    for (const r of rows) {
                        if (!r.st.checkedAt) { oldest = 0; break; }
                        oldest = oldest
                            ? Math.min(oldest, r.st.checkedAt) : r.st.checkedAt;
                    }
                    let t = '';
                    if (oldest) {
                        try { t = new Date(oldest).toLocaleTimeString(); }
                        catch (_) {}
                    }
                    win.checkedEl.textContent = oldest
                        ? ('checked ' + t) : 'checking…';
                    win.checkedEl.title = rows.length > 1
                        ? 'the oldest of these checks — each broker is asked, '
                            + 'and answers, separately'
                        : '';
                }

                // Idempotent: rebuild from the records every call. ONE ROW PER
                // CONFIGURED HOST — this window is where the question "which
                // broker?" is answered in full, so it names every one of them
                // and gives each its own state in words rather than describing
                // whichever broker the module last happened to look at.
                //
                // Rows come from hostRows(), the same reader the chip uses, so
                // the chip and the window cannot disagree about a host: there is
                // one derivation of "what state is this broker in", not two.
                function renderWindow(win) {
                    if (!win || win.disposed) return;
                    const rows = hostRows();
                    const agg = aggregate(rows);
                    const one = rows.length === 1;
                    if (win.checkedEl) renderChecked(win, rows);
                    const body = win.body;
                    body.innerHTML = '';

                    // The headline. With one broker it is the sentence this
                    // window has always opened with; with several it is the
                    // chip's own aggregate, so the two surfaces say the same
                    // thing in the same words. Banded from the WORST state
                    // either way, which for a single host is that host's.
                    const headline = addRow(body, 'Status', one
                        ? (({
                            'current': 'up to date',
                            'behind': 'a newer build is available',
                            'ahead-or-diverged':
                                'this checkout is ahead of upstream',
                            'unknown': 'could not be established',
                        })[state(rows[0].st)] || 'could not be established')
                        : agg.text, 'app-upd-' + bandFor(agg.worst));
                    headline.classList.add('app-upd-headline');

                    // The shared facts. Read from the LOCAL broker's record on
                    // purpose: the upstream repository is one constant for every
                    // broker in the list, and the comparison this browser can
                    // put a link on is the one the serving broker ran. A peer's
                    // own upstream row would be the same repository read a few
                    // hours apart — noise per host, not information.
                    const localSt = checkStateFor(LOCAL_HOST_ID);
                    const localChk = localSt.check;
                    if (localChk && localChk.repo) {
                        addRow(body, 'Tracking', localChk.repo);
                    }
                    const up = localChk && localChk.upstream;
                    if (up) {
                        addRow(body, 'Upstream', up.tag
                            || (up.sha ? String(up.sha).slice(0, 10) : '—')
                            + (up.branch ? ('  on ' + up.branch) : ''));
                    }

                    // ---- restart THIS broker (#183) ----
                    // Placed here, beside the shared facts about the local
                    // build that motivate it, and NOT inside the per-broker
                    // list below: the window names every configured host,
                    // but this control is about exactly one of them,
                    // always, regardless of which rows follow.
                    addHead(body, 'Restart');
                    renderRestartRow(body);

                    // ---- one row per broker ----
                    addHead(body, one ? 'This broker' : 'Brokers');
                    for (const r of rows) {
                        // The state in words first, because it is the answer;
                        // the build second, because it is the evidence. Both
                        // through addRow, i.e. through .textContent: the label
                        // is user-entered and the version and sha came off the
                        // network.
                        const detail = [r.words];
                        const loc = (r.st.check && r.st.check.local) || {};
                        if (loc.version || loc.sha) {
                            // The SHORT sha is what makes two builds of the same
                            // version tellable apart at a glance — a version
                            // alone cannot do it, which is the same reason
                            // 'no-git' is an unknown rather than a match.
                            detail.push(String(loc.version || 'unknown')
                                + (loc.sha
                                    ? ('  (' + String(loc.sha).slice(0, 10)
                                        + ')')
                                    : ''));
                        }
                        addRow(body, r.label + (r.hidden ? '  (hidden)' : ''),
                               detail.join('  ·  '),
                               'app-upd-' + bandFor(r.ps), 'app-upd-hostrow');
                    }

                    // Why each silent broker is silent. The most important text
                    // in the window: it is what stops a row that is not green
                    // from being read as "probably fine". Named with the broker
                    // it belongs to, because among N rows an unattributed reason
                    // belongs to nobody. 'pending' is skipped — a check still
                    // running is not a failure and has no reason to give.
                    for (const r of rows) {
                        if (answered(r.ps) || r.ps === 'pending') continue;
                        const code = reasonCode(r.st);
                        addNote(body, r.label + ' — ' + (REASONS[code]
                            || 'the reason was not reported') + '.',
                            'app-upd-why');
                    }

                    if (rows.some(function (r) {
                        return r.ps === 'ahead-or-diverged'; })) {
                        addNote(body, 'A checkout with commits upstream has '
                            + 'never seen is a development checkout, not a '
                            + 'stale one.');
                    }

                    // The link out, and it is the LOCAL broker's comparison:
                    // the url came from that broker's own check, so labelling it
                    // with anyone else's range would be a fabricated link. Built
                    // from that value plus our own literals — href is assigned,
                    // never innerHTML'd.
                    if (up && (up.url || up.tag)) {
                        const a = document.createElement('a');
                        a.className = 'app-upd-link';
                        a.target = '_blank';
                        a.rel = 'noopener noreferrer';
                        a.href = up.url || '';
                        a.textContent = up.tag
                            ? 'view this release on GitHub'
                            : 'view the commit range on GitHub';
                        if (a.href) body.appendChild(a);
                    }

                    // Two things this deliberately does NOT claim.
                    addNote(body, 'The working tree is not inspected: build ids '
                        + 'carry no dirty-tree marker, so local uncommitted '
                        + 'changes are invisible here and this reflects your '
                        + 'last commit only.', 'app-upd-caveat');

                    if (rows.some(function (r) { return r.ps === 'behind'; })) {
                        addNote(body, 'To update a broker that is behind: stop '
                            + 'it, run "git pull --ff-only" in its checkout, '
                            + 'reinstall dependencies if pyproject.toml '
                            + 'changed, then start it again and reload this '
                            + 'page.', 'app-upd-howto');
                    }
                }

                function renderAll() {
                    renderChip();
                    for (const w of openWins) renderWindow(w);
                }

                function launchUpdate() {
                    openAppWindow({ id: newAppId('upd'), appKind: 'update' });
                }
                function openOrFocusWindow() {
                    for (const w of windows.values()) {
                        if (w && w.appKind === 'update' && !w.disposed) {
                            openAppWindow({ id: w.id, appKind: 'update' });
                            return;
                        }
                    }
                    launchUpdate();
                }

                ctx.registerWindowKind({
                    appKind: 'update',
                    factory: function (d) { return openUpdateWindow(d); },
                    menu: {
                        label: 'Update check',
                        iconKey: 'update',
                        launch: function () { return launchUpdate(); },
                    },
                });
                // Teardown — registered AFTER registerWindowKind so LIFO runs it
                // FIRST: stop the timer and close any live window WHILE the kind
                // is still registered.
                ctx.onUnload(function () {
                    // Also stops the restart wait loop from polling (and
                    // repainting closed windows) once the mod itself is
                    // gone, rather than letting it run to its own bounded
                    // timeout in the background of a mod that is no longer
                    // loaded.
                    restartOpDead = true;
                    stop();
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app' && w.appKind === 'update') {
                            closeWindow(w.id);
                        }
                    }
                });

                renderChip();
                start();
                pollTick();
            },
        });
