        // ---- mod: update (#182) -------------------------------------------
        // Is the build you are looking at current with upstream? Finding that
        // out otherwise means opening a shell, cd-ing to the checkout and
        // running git fetch.
        //
        // Ships DISABLED (defaultEnabled:false) like aistatus, and for the same
        // reason: nothing here runs at top level except registerMod(), because
        // a top-level side effect would defeat the default-off contract. The
        // broker keeps its own `update_check_enabled` gate on top and answers
        // 503 until it is opted in, so the mod's switch alone has never been
        // what protects the network.
        //
        // What the two gates mean TOGETHER changed once (#182): ticking this mod
        // on in the Control Panel now asks the local broker to open its gate,
        // because being made to edit a JSON file and restart the process for a
        // decision you just expressed in the UI is friction, not safety. The
        // CLICK is what asks — never a page load, a synced preference or a
        // broker-side pin (see offerConsent). A broker that was never opted in
        // and is not being asked still gets you an honest "checking is switched
        // off here", not a silent egress.
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
                        + 'here and not a network problem — it is switched on '
                        + 'from that broker’s own desktop, or by an operator '
                        + 'setting "update_check_enabled" in its config',
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
                //   lastDeploy the `last_deploy` object riding this host's
                //              last good check, or null (#182 Part 2, A29).
                //              Cleared wherever `check` is nulled: a deploy
                //              outcome must not outlive the answer that
                //              carried it, exactly like the check itself.
                const hostChecks = new Map();   // hostId -> check state record
                function checkStateFor(hostId) {
                    let st = hostChecks.get(hostId);
                    if (!st) {
                        st = { hostId: hostId, check: null, error: null,
                               checkedAt: 0, inFlight: false,
                               lastDeploy: null,
                               refreshRefused: null };
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
                // answered/stateWords/WORST_FIRST moved to
                // mods/update/update-policy.js (#182 Part 2, A6) — the
                // same shared-scope split RESTART_REASONS took in A4.
                // Pure over their arguments, so hostRows/renderWindow
                // call them exactly as before; only WHERE they are
                // defined changed.
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
                // aggregate/chipLabel moved to update-policy.js too (A6):
                // both are pure over the rows/record they are handed, and
                // the fleet harness runs the shipped copies either way.

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
                        // When this READ began. Only a re-read that actually
                        // went to the broker can supersede a write we performed
                        // (see lastWrite) -- and only if it started AFTER it.
                        // The cheap "cache already has a record" path below is
                        // NOT a read of anything, so retiring a write there
                        // would replace a fresh answer with a stale cache entry
                        // nobody re-fetched.
                        const readSeq = ++opSeq;
                        // Deliberately NOT gated on modCatalogFetching. That set
                        // marks an in-flight GET but carries no promise to wait
                        // on, so skipping on it would leave us with no record
                        // and report a live broker as unreachable. A duplicate
                        // /info costs one request; a wrong verdict is the bug
                        // this whole mod is written around.
                        try { await fetchModCatalog(host); } catch (_) {}
                        const lw = lastWrite.get(host.id);
                        if (lw && readSeq > lw.seq) lastWrite.delete(host.id);
                    }
                    return capabilityFrom(effectiveRecord(host.id));
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
                        st.lastDeploy = null;
                        st.refreshRefused = null;
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
                            st.lastDeploy = null;
                            st.refreshRefused = null;
                            st.checkedAt = Date.now();
                        } else {
                            // "Check now" says refresh=1, the 30-minute tick
                            // never does. Without it the button re-requested
                            // an answer the broker had cached for a DAY, so a
                            // commit pushed in the last 24h stayed invisible
                            // and the apply had no fresh sha to act on. The
                            // broker floors and budgets these; a refusal comes
                            // back as a 200 that says so (refreshed:false).
                            const r = await hostFetch(
                                host,
                                '/update/check'
                                    + ((opts && opts.refresh) ? '?refresh=1' : ''),
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
                                st.lastDeploy = null;
                                st.refreshRefused = null;
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
                                st.lastDeploy = null;
                                st.refreshRefused = null;
                            } else if (!r.ok) {
                                throw new Error('HTTP ' + r.status);
                            } else {
                                const j = await r.json();
                                if (!j || !j.ok || !j.check) {
                                    throw new Error('bad-response');
                                }
                                st.check = j.check;
                                st.error = null;
                                // A refused refresh must never read as "just
                                // checked": the answer is honest, but it is
                                // as old as its own checkedAt, and the button
                                // promised otherwise. Recorded only when the
                                // broker actually refused one, and cleared on
                                // every other answer so it cannot outlive the
                                // click that earned it.
                                st.refreshRefused =
                                    (opts && opts.refresh && j.refreshed === false
                                     && j.refresh_refused)
                                        ? j.refresh_refused : null;
                                // #182 Part 2 (A29): the finalized deploy
                                // outcome rides BESIDE the check (app.py
                                // _update_check), so it is adopted and
                                // dropped in the same breath as the answer
                                // it came with. Walked as untrusted input
                                // later, in deployOutcome; only ever SHOWN
                                // for the local broker (deployStrip).
                                st.lastDeploy = (j.last_deploy
                                    && typeof j.last_deploy === 'object')
                                    ? j.last_deploy : null;
                            }
                            st.checkedAt = Date.now();
                        }
                    } catch (e) {
                        // Degrade to unknown — never to "current".
                        st.check = null;
                        st.error = failure;
                        st.lastDeploy = null;
                        st.refreshRefused = null;
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
                    // #182: the same GC for this mod's other per-host maps.
                    // A failed or locked write leaves its note behind on
                    // purpose (it is what the row is reporting), so without
                    // this a removed broker's error would be inherited by
                    // whatever host next gets handed its id.
                    // policyOps is keyed hostId + '|' + kind (A5), so the
                    // host id is parsed back out of the key. The kind never
                    // carries a '|', so the last one always ends the id.
                    for (const key of Array.from(policyOps.keys())) {
                        const cut = key.lastIndexOf('|');
                        const hid = cut === -1 ? key : key.slice(0, cut);
                        if (!ids.has(hid)) policyOps.delete(key);
                    }
                    for (const id of Array.from(lastWrite.keys())) {
                        if (!ids.has(id)) lastWrite.delete(id);
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
                    // Feature-detected: a runtime-installed copy of this mod
                    // can run against an older core with no ctx.visibility.
                    // Either way `timer` holds a {stop}-shaped handle.
                    timer = ctx.visibility
                        ? ctx.visibility.pausableInterval(pollTick, POLL_MS)
                        : (function () {
                            const id = setInterval(pollTick, POLL_MS);
                            return { stop: function () { clearInterval(id); } };
                        })();
                }
                function stop() {
                    if (timer) { timer.stop(); timer = null; }
                }

                // ---- opting THIS broker in (#182) --------------------------
                // Turning update checking on used to mean editing
                // broker_config.json and restarting the process. The broker now
                // takes the decision over POST /update/policy and applies it
                // live, and this is the client half.
                //
                // WHAT COUNTS AS CONSENT, because this grants egress and that is
                // not a thing to infer. Exactly two events reach the route:
                //
                //   1. `ctx.enabledByUser` — somebody ticked this mod on in the
                //      Control Panel, in this browser, just now. The loader sets
                //      that flag ONLY on the setModEnabled path (86, initMod),
                //      so a page load, a synced or restored preference, a
                //      dependency cascade, and a broker-side mod pin applied
                //      after login all arrive with it false. That matters: this
                //      mod shipped promising that enabling it caused no egress,
                //      so re-reading an old stored preference as fresh consent
                //      would be retroactive — and a remote operator who can pin
                //      mods on a broker could otherwise make that broker start
                //      making outbound requests without touching it.
                //   2. The button below, clicked.
                //
                // NOTHING SENDS `false` AUTOMATICALLY, and that asymmetry is
                // deliberate rather than an omission. The gate belongs to the
                // BROKER while the mod's on/off switch belongs to a BROWSER, so
                // a browser with the mod off that posted `false` would revoke a
                // grant some other browser is actively relying on, and the two
                // would fight every time either one loaded. Turning the mod off
                // stops this browser drawing anything; the Stop button is how
                // the grant itself is given back.
                //
                // ANY broker in the list, not just the serving one. The first
                // cut of this was local-only, which turned out to be the one
                // shape that does not work: an operator administers a fleet
                // from ONE desktop, so the broker that needs switching on is
                // exactly the broker they have no local session on. The route
                // is token-gated at the far end and no longer origin-gated —
                // see app.py — so a peer is asked with that peer's own saved
                // token, the same way its version check already is.
                //
                // The AUTOMATIC path stays local-only regardless (offerConsent):
                // ticking this mod on in THIS browser is not a decision about a
                // remote machine's egress. A peer is only ever changed by its
                // own row's button being clicked.
                // Keyed hostId + '|' + kind, where the kind names which row
                // the write reports into ('check' for the checking switch;
                // A6's self-update row brings its own) — so one broker can
                // carry two independent write notes without either
                // clobbering the other's.
                const policyOps = new Map();  // key -> {phase, note, want}
                let consentSent = false;  // one attempt per page load, ever
                // A write is authoritative about a broker until something READ
                // that broker afterwards. Without this, a poll already in flight
                // when the write lands finishes last and puts the pre-write
                // /info back in the shared cache, so a switch you just threw
                // flips back on screen until the next tick.
                //
                // `opSeq` is a plain monotonic counter over both operations. A
                // poll records its own start; on finishing it may only clear a
                // write it STARTED AFTER. One that began earlier is stale by
                // definition and leaves the write standing.
                let opSeq = 0;
                const lastWrite = new Map();   // hostId -> {update, seq, fp}

                // What a broker last told /info about its own update capability,
                // or null if it has not answered yet or runs a build too old to
                // publish one. Read out of the control panel's shared record for
                // the same reason the capability probe is — one cache, so two
                // surfaces cannot disagree about one broker.
                // The shared /info record for a broker, with any write WE
                // performed since the last read of it overlaid. ONE reader, used
                // by both the capability probe and the switch, because two
                // readers is how a window ends up offering "Enable" on a broker
                // its own chip has already started checking.
                //
                // The overlay rather than patching the cached object in place:
                // a poll already in flight when the write lands finishes last
                // and replaces that object wholesale, silently undoing the
                // patch. lastWrite is retired deliberately, by a read that
                // began after it (see capabilityFor).
                //
                // Guarded on the host FINGERPRINT, so an id since re-pointed at
                // a different machine cannot inherit the old one's answer.
                function effectiveRecord(hostId) {
                    const hid = hostId || LOCAL_HOST_ID;
                    const rec = modCatalogCache.get(hid);
                    const lw = lastWrite.get(hid);
                    if (!lw || lw.fp !== hostFingerprint(hid)) return rec;
                    return Object.assign({}, rec || {}, { update: lw.update });
                }
                function updateCapFor(hostId) {
                    const rec = effectiveRecord(hostId);
                    const upd = rec && rec.update;
                    return (upd && typeof upd === 'object') ? upd : null;
                }
                // Can this browser change that broker's gate? `mutable` is false
                // when its broker_config.json NAMES update_check_enabled: that
                // operator's file wins, on purpose, so editing it and restarting
                // stays the reliable way to stop unwanted egress. ABSENT reads as
                // "cannot", which is what keeps an older peer from being asked at
                // all: the key only exists on a build that also has the route, so
                // its absence is proof the POST would die in that broker's
                // cross-origin preflight and come back as an opaque network error
                // — the same reasoning the capability probe already runs on.
                function policyMutableFor(hostId) {
                    const upd = updateCapFor(hostId);
                    if (!upd || upd.mutable !== true) return false;
                    // The serving broker ships with this page, so its build is
                    // ours by construction and needs no capability handshake.
                    if ((hostId || LOCAL_HOST_ID) === LOCAL_HOST_ID) return true;
                    // A PEER must additionally say it accepts a cross-origin
                    // write. `mutable` alone is not that claim: the first build
                    // to ship this route origin-gated it, so it reports
                    // mutable:true and answers 403 -- offering a switch there
                    // would produce a refusal we could only describe wrongly.
                    return upd.remote_writable === true;
                }
                // A stable-ish fingerprint of WHICH machine an id points at, so
                // a result can be checked against the host it was asked of. An
                // id is reusable: remove a broker, add a different one, and the
                // registry may hand out the same id, at which point an in-flight
                // answer from the old machine would be applied to the new one's
                // row. The url is what actually names the box.
                function hostFingerprint(hostId) {
                    const h = updHost(hostId);
                    return h ? String(h.url || '') : null;
                }

                // opts.poll defaults ON: a click must produce the answer in the
                // same beat, not at the next half-hourly tick. offerConsent
                // turns it off because a full fleet pollTick follows it
                // immediately, and two checks of the same broker one line apart
                // is a wasted request however cheap the second one is.
                //
                // opts.kind names which row's op this write reports into
                // (default 'check', the checking switch; A6's self-update
                // row passes its own) — policyOps is keyed on it, so two
                // rows about one broker never overwrite each other's notes.
                //
                // Takes a host ID, never a host object, and resolves it HERE —
                // the same rule the poll follows, and for the same reason: a
                // host object captured before an await can name a broker whose
                // url or token has since been re-entered, or one that has been
                // removed outright. A null host must never reach hostFetch,
                // which would silently aim this write at the SERVING origin and
                // switch on the wrong machine.
                async function setPolicy(hostId, changes, opts) {
                    // No falsy-id fallback. Defaulting a missing id to the local
                    // broker is precisely the wrong-machine bug this function is
                    // written to avoid -- it would aim a write meant for a peer
                    // at the box serving the page. Callers name their host.
                    if (typeof hostId !== 'string' || !hostId) return false;
                    const hid = hostId;
                    const kind = (opts && opts.kind) || 'check';
                    const opKey = hid + '|' + kind;
                    const wantKeys = Object.keys(changes || {});
                    // What the row's busy label renders from. The check row's
                    // write derives it from the one key it posts; every OTHER
                    // caller names its direction, and its own busy words, via
                    // opts — the self-update row (A6) because its body never
                    // carries check_enabled, and the consent write because its
                    // body MAY not (a check already on, config-pinned or a
                    // standing grant, is rightly omitted — deriving direction
                    // from the absent key would read that grant as a revoke).
                    const wantOn = (opts && typeof opts.want === 'boolean')
                        ? opts.want : !!(changes && changes.check_enabled);
                    const mark = function (phase, note) {
                        policyOps.set(opKey, { phase: phase, note: note,
                                               want: wantOn });
                        renderAll();
                    };
                    const host = updHost(hid);
                    if (!host) {
                        mark('failed', 'that broker is no longer configured');
                        return false;
                    }
                    // Captured BEFORE the await, compared after: if this id has
                    // been re-pointed at a different machine meanwhile, the
                    // answer belongs to a broker that is no longer in this row.
                    const fp = hostFingerprint(hid);
                    mark('busy', (opts && opts.busyNote)
                        || (wantOn ? 'switching checking on…'
                            : 'switching checking off…'));
                    let resp;
                    try {
                        resp = await hostFetch(host, '/update/policy', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify(changes),
                            timeoutMs: 15000,
                        });
                    } catch (_) {
                        mark('failed', 'could not reach that broker to ask');
                        return false;
                    }
                    let body = null;
                    try { body = await resp.json(); } catch (_) {}
                    // The row this answer belongs to may not exist any more.
                    // Say nothing rather than writing another broker's outcome
                    // into it; the write itself already landed on the machine we
                    // asked, which is the correct outcome for a host that has
                    // since been removed or re-entered.
                    if (hostFingerprint(hid) !== fp) {
                        policyOps.delete(opKey);
                        renderAll();
                        return false;
                    }
                    // The interpretation is pure and shared — see
                    // policyWriteOutcome in update-policy.js. The single-key
                    // check sentences are byte-for-byte what this machinery
                    // said before it generalized (A5).
                    const out = policyWriteOutcome(resp.status, body,
                                                   wantKeys);
                    // What the write returned is authoritative about this broker
                    // until something reads it again — held HERE rather than
                    // patched into the shared /info record, because a poll that
                    // was already in flight finishes last and would put the
                    // pre-write answer back. See updateCapFor / lastWrite.
                    // Installed on REFUSALS too (A6): a 409 policy_locked
                    // carries the broker's own authoritative view, and every
                    // row must repaint from what it just said rather than
                    // from the stale cache the click was drawn against.
                    if (body && body.update
                            && typeof body.update === 'object') {
                        lastWrite.set(hid, { update: body.update,
                                             seq: ++opSeq, fp: fp });
                    }
                    if (!out.ok) {
                        mark(out.phase, out.note);
                        return false;
                    }
                    policyOps.delete(opKey);
                    if (!opts || opts.poll !== false) {
                        await poll(hid, {});
                    }
                    renderAll();
                    return true;
                }

                // The checking switch, exactly as it always behaved: one
                // gate, one key, same op key ('check') and the same words.
                // Kept as the named entry point because the row's click
                // handlers call it — and the body it builds is the shape
                // the no-auto-revoke guard pins: the value is always
                // derived from what a human just asked for, never a
                // hardcoded literal.
                async function setChecking(hostId, want, opts) {
                    return setPolicy(hostId, { check_enabled: !!want }, opts);
                }

                // The consent path. Runs at most once per page load, only when a
                // human just ticked this mod on, and only when the local broker
                // has actually said something is closed and ours to change — an
                // unconditional POST would spend a request on every broker that
                // already had everything on.
                //
                // ONE request, all three gates (A5): the click that consents
                // to checking consents to the broker keeping itself up to
                // date, so every gate the broker will let this browser open
                // — mutable and not already open, per consentBody — rides
                // the same POST. A gate the config owns is silently left to
                // its file, and a stored "off" the sidecar merely
                // synthesized for the check is granted like a default, not
                // honoured like a revoke. When the serving view carries no
                // `policy` block this degrades to the single-key write it
                // always was — unreachable live, since the serving broker
                // runs this build by construction, but the fleet model
                // exercises it through policyKeysFor.
                async function offerConsent() {
                    if (consentSent) return;
                    consentSent = true;
                    const host = updHost(LOCAL_HOST_ID);
                    if (!host) return;
                    try { await capabilityFor(host, false); } catch (_) {}
                    const upd = updateCapFor(LOCAL_HOST_ID);
                    const keys = policyKeysFor(upd);
                    if (keys.length > 1) {
                        const grants = consentBody(upd.policy);
                        if (!Object.keys(grants).length) return;
                        // want/busyNote are NAMED, never derived: this body
                        // omits check_enabled whenever the check is already
                        // on, and a consent is always a grant — without these
                        // a failed write would strand a checking-off note on
                        // a row that was never being switched off.
                        await setPolicy(LOCAL_HOST_ID, grants,
                                        { poll: false, want: true,
                                          busyNote: 'granting what the '
                                              + 'enable click consented '
                                              + 'to…' });
                        return;
                    }
                    // The single-key degradation: the exact pre-A5 path.
                    if (keys.length !== 1) return;
                    if (upd.check_enabled !== false) return;
                    if (upd.mutable !== true) return;
                    await setChecking(LOCAL_HOST_ID, true, { poll: false });
                }

                // ---- apply / post-apply pure helpers moved out ------------
                // applyTargetSha/applyGateFromFacts/applyGateWords/
                // applyRefusalOutcome (#182 Part 2, atom A30) and
                // deployOutcome/shortSha/staleSurvivors/deployStrip (#182
                // Part 2, A29) now live in mods/update/update-apply.js --
                // same mod, spliced immediately before this file in ui.py's
                // _MODS (the same split editor.js/codemirror.js already use,
                // #146). One shared inline <script>, so a plain top-level
                // declaration there is a name this closure can still call
                // exactly as before; only WHERE they are defined changed.
                // freshTerminalHost stays HERE (below) because it is not
                // pure -- it reads this closure's own updHost/LOCAL_HOST_ID.

                // Where a fresh terminal goes: the LOCAL broker, resolved at
                // CLICK time from the literal id — the same rule every action
                // in this mod follows. The null matters: launchProfile
                // defaults a missing host positionally to whoever served the
                // page, which is exactly the fallback this mod bans, so a
                // caller getting null here must stop instead of falling
                // through to it.
                function freshTerminalHost() {
                    return updHost(LOCAL_HOST_ID);
                }

                // ---- the self-update row's non-DOM half (A6) --------------
                // Offered for any build a policy write could reach: a
                // modern peer shows its state even when config owns
                // everything (a standing grant must never be invisible),
                // the flat single-key build gets a dead row that says to
                // update it, and a peer with no update view at all gets
                // nothing — there is no fact to report.
                function selfUpdateRowNeeded(hostId) {
                    if (policyOps.get(hostId + '|self')) return true;
                    return policyKeysFor(updateCapFor(hostId)).length > 0;
                }
                // ONE derivation of the row's model for a host: the
                // `policy` block when the view carries the key (a
                // malformed block fails closed inside the model — never
                // a fallback to the flat fields), the flat build's null
                // otherwise, plus this host's own 'self'-kind op.
                function selfUpdateModelFor(hostId) {
                    const upd = updateCapFor(hostId);
                    const pol = (upd && 'policy' in upd)
                        ? upd.policy : null;
                    return selfUpdateRowModel(pol,
                        policyOps.get(hostId + '|self') || null);
                }
                // The post-dialog half of the remote enable. The dialog
                // named a machine (url + label captured before it
                // opened); if the id now resolves elsewhere — or to
                // nothing — the confirmation belongs to a machine no
                // longer in that row, so nothing is sent and the row
                // says so. The grant itself is REBUILT from the current
                // facts: a stale paint's keys are never replayed, and a
                // row that turned ON meanwhile finishes with NO post —
                // an enable confirmation never becomes a Stop.
                async function commitRemoteSelfUpdate(hostId, url, label) {
                    const now = updHost(hostId);
                    if (!now || String(now.url || '') !== String(url || '')
                            || String(now.label || '')
                                !== String(label || '')) {
                        policyOps.set(hostId + '|self', {
                            phase: 'failed', want: true,
                            note: 'that broker changed while the '
                                + 'confirmation was open — nothing was '
                                + 'sent' });
                        renderAll();
                        return false;
                    }
                    const m = selfUpdateModelFor(hostId);
                    if (m.on || m.disabled || !m.postKeys.length) {
                        return false;
                    }
                    return setPolicy(hostId,
                        policyChangesFor(m.postKeys, true),
                        { kind: 'self', want: true,
                          busyNote: selfUpdateBusyNote(true) });
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
                // ---- restart-reason words moved out ------------------------
                // RESTART_REASONS/restartReasonWords (#182 Part 2, atom A4)
                // now live in mods/update/update-policy.js -- same mod,
                // spliced immediately before update-apply.js in ui.py's
                // _MODS (policy -> apply -> this file). One shared inline
                // <script>, so a plain top-level declaration there is a
                // name this closure can still call exactly as before; only
                // WHERE it is defined changed.

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
                                 reason: null, retryAfterS: null,
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
                        // Only the cooldown reason ever carries this (#183
                        // R6); walked as untrusted like everything else here.
                        retryAfterS: num(r.retry_after_s) || null,
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

                // The polling loop lives in applyFlow.pollBootId (#182
                // Part 2, A30/A3 -- an apply ends in this same wait, so
                // update-apply.js's makeApplyFlow carries it): resolves
                // the host fresh every iteration, exactly as this loop
                // always did, since the very broker being asked is
                // expected to disappear and come back mid-wait. A
                // transport failure there is THE expected shape of this
                // loop, not a fault -- the broker is mid-stop or
                // mid-relaunch and simply not there to answer. The null
                // fingerprint keeps this restart flow exactly as it was:
                // it predates A3's machine pin.
                async function waitForNewBootId(beforeBootId) {
                    restartOp = { phase: 'waiting', note: 'restarting…' };
                    renderAll();
                    const res = await applyFlow.pollBootId(LOCAL_HOST_ID,
                        beforeBootId, null, () => restartOpDead);
                    if (restartOpDead || res === 'dead') return;
                    if (res === 'no-host') {
                        restartOp = { phase: 'failed',
                            note: 'the local broker is no longer '
                                + 'configured' };
                    } else if (res === 'changed') {
                        // THE proof. Not the 202 earlier, and not this
                        // response merely arriving — the identity changed,
                        // the one fact a response from the process being
                        // replaced can never assert about itself. Stale the
                        // instant it did: everything the cached record
                        // described belonged to the broker that just
                        // stopped existing.
                        restartOp = { phase: 'done',
                            note: 'restarted — this build is now live' };
                        modCatalogCache.delete(LOCAL_HOST_ID);
                        renderAll();
                        recheck();
                        return;
                    } else {
                        restartOp = { phase: 'timeout',
                            note: 'this broker did not come back within '
                                + Math.round(RESTART_WAIT_TIMEOUT_MS / 1000)
                                + 's. It may still be starting, or it may need '
                                + 'attention on the machine itself — this '
                                + 'window cannot tell which.' };
                    }
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
                                + restartReasonWords(code,
                                    body && body.retry_after_s) };
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
                        if (cont.guaranteed || cont.unknown) {
                            // #182 Part 2 (A29): reconnecting is not
                            // updating. One sentence, beside the count it
                            // is about — after an apply-restart these are
                            // exactly the sessions #22 flags as stale
                            // (their build no longer matches the broker's),
                            // and the window's post-apply strip counts them
                            // by the same rule.
                            line('A session that survives keeps running '
                                + 'the code it was started with — if this '
                                + 'restart brings up a new build, it stays '
                                + 'on the old one until relaunched by hand.');
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

                // Is there a switch to offer for THIS broker? Yes whenever its
                // gate is off (so it can be turned on, or so the window can say
                // whose decision it is when it cannot), and whenever we hold a
                // stored grant that can be given back. No when checking is on
                // because a config file says so: there is nothing to offer, and
                // the row above already reports the state.
                //
                // Per host, because the fleet is the point — the broker that
                // needs switching on is usually not the one serving this page.
                function policyRowNeeded(hostId) {
                    const upd = updateCapFor(hostId);
                    // The 'check'-kind op only: another kind's write (A6's
                    // self-update row) must not conjure a checking row.
                    if (policyOps.get(hostId + '|check')) return true;
                    if (!upd) return false;         // too old, or not answered
                    if (upd.check_enabled === false) return true;
                    return upd.source === 'stored';
                }

                // Names the machine, not just the label. A label is
                // user-entered, may be duplicated across rows, and can be long or
                // deliberately confusable — so the URL, which is what actually
                // decides where the request goes, is shown alongside it. Both go
                // in as .textContent.
                function confirmRemoteEnable(hostId, label) {
                    const host = updHost(hostId);
                    const url = host ? String(host.url || '') : '';
                    return openDialog({
                        title: 'Let that broker check for updates?',
                        body: function (c) {
                            const msg = document.createElement('div');
                            msg.className = 'app-dialog-msg';
                            msg.textContent = 'This switches update checking on '
                                + 'for another machine, not the one serving this '
                                + 'page.';
                            c.appendChild(msg);
                            const list = document.createElement('div');
                            list.className = 'app-upd-restart-continuity';
                            const line = function (t, cls) {
                                const d = document.createElement('div');
                                d.className = 'app-upd-restart-cline'
                                    + (cls ? ' ' + cls : '');
                                d.textContent = t;
                                list.appendChild(d);
                            };
                            line(label);
                            if (url) line(url);
                            line('It will contact GitHub, which discloses that '
                                + 'machine’s address to GitHub. Switching '
                                + 'checking off later does not undo a disclosure '
                                + 'already made.', 'app-upd-restart-warn');
                            c.appendChild(list);
                        },
                        buttons: [
                            { label: 'Enable checking', value: true,
                              primary: true },
                            { label: 'Cancel', value: false },
                        ],
                    }).then(function (res) {
                        if (!res || !res.value) return false;
                        // Re-checked AFTER the dialog: the fleet list can change
                        // while it is open, and a confirmation is for the machine
                        // that was named in it.
                        return updHost(hostId)
                            && String(updHost(hostId).url || '') === url;
                    });
                }

                // A6: the remote self-update confirm. Same skeleton as
                // confirmRemoteEnable above — machine named by label AND
                // url, re-verified after the dialog — but the grant is
                // rebuilt from the CURRENT facts by commitRemoteSelfUpdate
                // instead of trusting the paint the click came from. One
                // pending confirm per host, ever.
                const selfConfirms = new Set();
                function confirmRemoteSelfUpdate(hostId, label) {
                    const host = updHost(hostId);
                    if (!host || selfConfirms.has(hostId)) {
                        return Promise.resolve(false);
                    }
                    const url = String(host.url || '');
                    const w = selfUpdateConfirmWords(label, url);
                    selfConfirms.add(hostId);
                    return openDialog({
                        title: w.title,
                        body: function (c) {
                            c.appendChild(mkEl('div', 'app-dialog-msg',
                                w.intro));
                            const list = mkEl('div',
                                'app-upd-restart-continuity');
                            for (const t of w.lines) {
                                list.appendChild(mkEl('div',
                                    'app-upd-restart-cline', t));
                            }
                            list.appendChild(mkEl('div',
                                'app-upd-restart-cline app-upd-restart-warn',
                                w.warning));
                            c.appendChild(list);
                        },
                        buttons: [
                            { label: w.okLabel, value: true,
                              primary: true, danger: true },
                            { label: 'Cancel', value: false },
                        ],
                    }).then(function (res) {
                        selfConfirms.delete(hostId);
                        if (!res || !res.value) return false;
                        return commitRemoteSelfUpdate(hostId, url, label);
                    }, function (e) {
                        selfConfirms.delete(hostId);
                        throw e;
                    });
                }

                // One broker's switch plus its inline reason, rebuilt from
                // policyOps + the shared /info record on every pass exactly like
                // renderRestartRow — nothing is held on the element, so a poll
                // landing mid-write cannot leave a stale label behind.
                //
                // `local` changes only the WORDS. Acting on the machine you are
                // sitting at and acting on one across the network are different
                // enough that the control should not describe them identically,
                // and "this broker" on a row about someone else's machine is the
                // kind of quiet mislabel that gets the wrong box switched on.
                function renderPolicyRow(body, hostId, label) {
                    const local = (hostId || LOCAL_HOST_ID) === LOCAL_HOST_ID;
                    const upd = updateCapFor(hostId);
                    const op = policyOps.get(hostId + '|check');
                    const on = !!(upd && upd.check_enabled === true);
                    const busy = !!(op && op.phase === 'busy');
                    // While a write is in flight the label comes from what was
                    // ASKED FOR, never from the latest cached state: a poll that
                    // observes the new value first would otherwise leave an
                    // enable reading "Stopping…" beside "switching checking on…".
                    const doing = busy ? !!op.want : !on;
                    const mutable = policyMutableFor(hostId);
                    const which = local ? 'this broker' : label;
                    const row = document.createElement('div');
                    row.className = 'app-upd-restart-row';
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'app-upd-restart-btn';
                    // .textContent throughout: `label` is user-entered text.
                    btn.textContent = busy
                        ? (doing ? 'Enabling…' : 'Stopping…')
                        : (on ? 'Stop checking on ' + which
                            : 'Enable checking on ' + which);
                    btn.title = on
                        ? 'that broker stops contacting GitHub. Its address has '
                            + 'already been disclosed by the checks it has run — '
                            + 'this stops future ones'
                        : 'lets that broker contact GitHub to compare its build '
                            + 'against upstream. No other broker is affected';
                    btn.disabled = busy || !mutable;
                    btn.addEventListener('mousedown', function (e) {
                        e.stopPropagation();
                    });
                    btn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        // Enabling a REMOTE machine's egress is confirmed;
                        // enabling the local one is not, and stopping either is
                        // not. The asymmetry is about what the click costs, not
                        // about authority: this is a repetitive list of similar
                        // rows, a mis-click lands on a box you are not looking
                        // at, and the first check discloses that box's address
                        // in a way no later Stop can retract. Stopping is
                        // reversible and local is where you already are.
                        if (!local && !on) {
                            confirmRemoteEnable(hostId, label).then(
                                function (yes) {
                                    if (!yes) return;
                                    return setChecking(hostId, true);
                                }).catch(function () {});
                            return;
                        }
                        setChecking(hostId, !on).catch(function () {});
                    });
                    row.appendChild(btn);
                    const status = document.createElement('span');
                    status.className = 'app-upd-restart-inline';
                    if (op) {
                        status.textContent = op.note;
                        if (op.phase === 'failed' || op.phase === 'locked') {
                            status.classList.add('app-upd-amber');
                        }
                    } else if (!mutable) {
                        // The button is dead, so say why rather than leaving a
                        // greyed control with no explanation beside it. Judged
                        // off the per-gate check facts when the `policy` block
                        // exists (A6); flat-field fallback for old peers. The
                        // words themselves are unchanged.
                        status.textContent = policyCheckSource(upd) === 'config'
                            ? 'its config names "update_check_enabled", so that '
                                + 'file decides'
                            : 'it has not reported whether the switch can be '
                                + 'changed from here';
                    } else if (on) {
                        status.textContent = 'switched on from here; it stays on '
                            + 'across restarts until it is switched off';
                    }
                    row.appendChild(status);
                    body.appendChild(row);
                    return row;
                }

                // A6: the second per-broker row — may this broker pull new
                // code and restart itself? apply + restart TOGETHER, never
                // check_enabled; the words and the transition feasibility
                // all live in selfUpdateRowModel (update-policy.js), this
                // is only the wiring.
                function renderSelfUpdateRow(body, hostId, label) {
                    const local = (hostId || LOCAL_HOST_ID)
                        === LOCAL_HOST_ID;
                    const upd = updateCapFor(hostId);
                    const op = policyOps.get(hostId + '|self');
                    const m = selfUpdateModelFor(hostId);
                    const busy = !!(op && op.phase === 'busy');
                    // A peer must additionally accept a cross-origin write
                    // at all — policyMutableFor's doctrine; the serving
                    // broker needs no handshake.
                    const writable = local
                        || !!(upd && upd.remote_writable === true);
                    const row = mkEl('div', 'app-upd-restart-row');
                    const btn = mkEl('button', 'app-upd-restart-btn',
                        m.labelWords);
                    btn.type = 'button';
                    btn.title = m.on
                        ? 'withdraws this broker’s permission to pull new '
                            + 'code and restart itself. Nothing already '
                            + 'applied is undone'
                        : 'lets this broker download code from GitHub and '
                            + 'restart itself when its Update button is '
                            + 'used';
                    btn.disabled = busy || m.disabled || !writable
                        || !m.postKeys.length;
                    btn.addEventListener('mousedown',
                        (e) => e.stopPropagation());
                    btn.addEventListener('click', function (e) {
                        e.stopPropagation();
                        if (btn.disabled) return;
                        // Enabling a REMOTE machine is confirmed; Stop and
                        // local are not — the checking row's asymmetry,
                        // for the same reasons.
                        if (!local && !m.on) {
                            confirmRemoteSelfUpdate(hostId, label)
                                .catch(function () {});
                            return;
                        }
                        const grant = !m.on;
                        setPolicy(hostId,
                            policyChangesFor(m.postKeys, grant),
                            { kind: 'self', want: grant,
                              busyNote: selfUpdateBusyNote(grant) })
                            .catch(function () {});
                    });
                    row.appendChild(btn);
                    const status = mkEl('span', 'app-upd-restart-inline',
                        m.note || '');
                    if (op && (op.phase === 'failed'
                            || op.phase === 'locked')) {
                        status.classList.add('app-upd-amber');
                    }
                    row.appendChild(status);
                    body.appendChild(row);
                    return row;
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
                            ? restartReasonWords(info.reason, info.retryAfterS)
                            : 'this broker has not reported a restart '
                                + 'capability yet';
                        status.classList.add('app-upd-grey');
                    }
                    row.appendChild(status);
                    body.appendChild(row);
                }

                // ---- update APPLY (#182 Part 2, atom A30; host-
                // parameterized in A3): the flow itself -- POST
                // /update/apply plus the boot-id wait that follows it --
                // is makeApplyFlow in mods/update/update-apply.js,
                // dependency-injected so it can target any EXPLICIT host
                // and so the node harness can execute it. This block only
                // builds its deps object, once; op state lives per host
                // inside the factory, so N targets can be busy
                // independently.
                let applyOpDead = false;
                const applyFlow = makeApplyFlow({
                    localHostId: LOCAL_HOST_ID,
                    updHost: updHost,
                    hostFingerprint: hostFingerprint,
                    hostFetch: hostFetch,
                    renderAll: renderAll,
                    modCatalogCache: modCatalogCache,
                    // Success for the LOCAL broker keeps its fleet-wide
                    // recheck(), exactly as before A3; any other host
                    // gets a targeted refresh poll of itself instead.
                    recheckHost: function (hid) {
                        return (hid === LOCAL_HOST_ID)
                            ? recheck() : poll(hid, { refresh: true });
                    },
                    // ONE unload flag stops every host's op at once --
                    // ctx.onUnload below flips it, as it always did.
                    isDead: function () { return applyOpDead; },
                    sleep: restartSleep,
                    now: function () { return Date.now(); },
                    waitTimeoutMs: RESTART_WAIT_TIMEOUT_MS,
                    pollMs: RESTART_POLL_MS,
                });

                function mkEl(tag, cls, text) {
                    const e = document.createElement(tag);
                    if (cls) e.className = cls;
                    if (text !== undefined) e.textContent = text;
                    return e;
                }

                // Commit range/count/compare link, then the SAME live-
                // session cost block restartConfirmBody renders (#183) --
                // an apply ends in that same restart.
                function applyConfirmBody(oldSha, targetSha, behindBy,
                                          compareUrl, cont) {
                    const restartPart = restartConfirmBody(cont);
                    return function (c) {
                        const range = (oldSha ? shortSha(oldSha) : 'unknown')
                            + '..' + (targetSha ? shortSha(targetSha)
                                : 'unknown');
                        c.appendChild(mkEl('div', 'app-dialog-msg', 'This '
                            + 'pulls ' + range + (typeof behindBy === 'number'
                                ? (' (' + behindBy + ' commit'
                                    + (behindBy === 1 ? '' : 's') + ')') : '')
                            + ' from the pinned upstream repository, then '
                            + 'restarts this broker to bring it into '
                            + 'effect.'));
                        if (compareUrl) {
                            const a = mkEl('a', 'app-upd-link',
                                'view this commit range on GitHub');
                            a.target = '_blank';
                            a.rel = 'noopener noreferrer';
                            a.href = compareUrl;
                            c.appendChild(a);
                        }
                        restartPart(c);
                    };
                }

                // Rebuilt fresh every renderWindow() pass, like
                // renderRestartRow beside it. The click handler recomputes
                // nothing it does not already have in scope from this same
                // paint -- a repaint always precedes a click on a control
                // that was enabled, and the server re-checks regardless.
                function renderApplyRow(body) {
                    const st = checkStateFor(LOCAL_HOST_ID);
                    const upd = updateCapFor(LOCAL_HOST_ID);
                    const info = restartInfo();
                    // This row is the LOCAL broker's; the flow keys op
                    // state per host, so read this host's and no other's.
                    const applyOp = applyFlow.opFor(LOCAL_HOST_ID);
                    const chk = st.check;
                    const target = applyTargetSha(chk);
                    const code = applyGateFromFacts(state(st), target,
                        !!(upd && upd.apply_enabled === true),
                        info.available);
                    const busy = !!(applyOp && applyOp.phase === 'waiting');
                    const row = mkEl('div', 'app-upd-restart-row');
                    const btn = mkEl('button', 'app-upd-restart-btn',
                        busy ? 'Applying…' : 'Update…');
                    btn.type = 'button';
                    btn.title = 'pulls the pinned commit range from '
                        + 'upstream and restarts THIS broker onto it — '
                        + 'never a remote host';
                    btn.disabled = busy || !!code;
                    btn.addEventListener('mousedown', (e) => e.stopPropagation());
                    btn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        if (code || busy) return;  // defensive; disabled already
                        // Captured HERE, carried UNCHANGED into the POST body
                        // -- a moved upstream is the server's preview-sha-
                        // mismatch to catch, never this code's to paper over.
                        const oldSha = (chk.local && chk.local.sha) || null;
                        const behindBy = (typeof chk.behindBy === 'number')
                            ? chk.behindBy : null;
                        const compareUrl = (chk.upstream && chk.upstream.url)
                            || null;
                        openDialog({
                            title: 'Apply this update?',
                            body: applyConfirmBody(oldSha, target, behindBy,
                                compareUrl, info.continuity),
                            buttons: [
                                { label: 'Apply and restart', value: true,
                                  primary: true, danger: true },
                                { label: 'Cancel', value: false },
                            ],
                        }).then((res) => {
                            if (!res || !res.value) return;
                            return applyFlow.performApply(
                                LOCAL_HOST_ID, target);
                        }).catch(() => {});
                    });
                    row.appendChild(btn);
                    const stat = mkEl('div', 'app-upd-restart-inline');
                    if (applyOp && (busy || applyOp.phase === 'done'
                            || applyOp.phase === 'timeout'
                            || applyOp.phase === 'failed')) {
                        for (const t of applyOp.note) {
                            stat.appendChild(mkEl('div', null, t));
                        }
                        if (applyOp.phase === 'done') {
                            stat.classList.add('app-upd-green');
                        } else if (applyOp.phase === 'timeout'
                                || applyOp.phase === 'failed') {
                            stat.classList.add('app-upd-amber');
                        }
                    } else if (code) {
                        // The apply gate's CURRENT source, read off this
                        // same paint's live view -- never cached, so the
                        // words derive from what is true right now (A7).
                        const applyPolicy = (upd && upd.policy
                            && typeof upd.policy === 'object')
                            ? upd.policy.apply : null;
                        stat.textContent = applyGateWords(code, info.known
                            ? restartReasonWords(info.reason, info.retryAfterS)
                            : 'this broker has not reported a restart '
                                + 'capability yet', applyPolicy);
                        stat.classList.add('app-upd-grey');
                    }
                    row.appendChild(stat);
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

                // The strip deployStrip feeds (#182 Part 2, A29). LOCAL by
                // construction: it reads exactly one record — the serving
                // broker's — and the session list is the core poll's own
                // last-good list for that same broker, read at PAINT time so
                // a session relaunched by hand falls out of the count on the
                // next repaint rather than being carried from a snapshot.
                // Every string lands via textContent; the detail sentence is
                // the broker's own text.
                function renderDeployStrip(body) {
                    const sessList = (typeof pollStateFor === 'function')
                        ? pollStateFor(LOCAL_HOST_ID).sessions : null;
                    const strip = deployStrip(
                        checkStateFor(LOCAL_HOST_ID), sessList);
                    if (!strip) return;
                    addHead(body, 'Last update');
                    const box = document.createElement('div');
                    box.className = 'app-upd-deploy ' + strip.cls;
                    for (const t of strip.lines) {
                        const ln = document.createElement('div');
                        ln.className = 'app-upd-deploy-line';
                        ln.textContent = t;
                        box.appendChild(ln);
                    }
                    if (strip.newTerminal) {
                        // A FRESH terminal on the new build — the one honest
                        // offer. Never a relaunch of a surviving session:
                        // after a restart the broker's in-memory registry is
                        // gone, so nothing here can recreate what a session
                        // held, and pretending otherwise would be worse than
                        // the staleness it papered over.
                        const row = document.createElement('div');
                        row.className = 'app-upd-restart-row';
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className =
                            'app-upd-restart-btn app-upd-newterm-btn';
                        btn.textContent = 'New terminal';
                        btn.title = 'opens a fresh terminal on this broker, '
                            + 'running the new build. Existing sessions are '
                            + 'never touched';
                        btn.addEventListener('mousedown', function (e) {
                            e.stopPropagation();
                        });
                        btn.addEventListener('click', function (e) {
                            e.stopPropagation();
                            const h = freshTerminalHost();
                            if (!h) return;
                            // The core's own (+) quick-launch path, aimed at
                            // the resolved local host and that host's default
                            // profile — the same terminal the (+) button
                            // itself would open.
                            Promise.resolve(
                                launchProfile(h, hostDefaultProfile(h)))
                                .catch(function () {});
                        });
                        row.appendChild(btn);
                        const note = document.createElement('span');
                        note.className = 'app-upd-restart-inline';
                        note.textContent = 'a fresh terminal starts on the '
                            + 'new build; surviving sessions keep their old '
                            + 'code until relaunched by hand';
                        row.appendChild(note);
                        box.appendChild(row);
                    }
                    body.appendChild(box);
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
                    // A broker that REFUSED the forced refresh is named here
                    // rather than left to imply it answered afresh: the words
                    // below say the answer is the one it already had, and the
                    // clock above says how old that is.
                    const refused = rows.filter(r => r.st.refreshRefused);
                    win.checkedEl.textContent = oldest
                        ? ('checked ' + t) : 'checking…';
                    if (refused.length) {
                        const w = refreshRefusedWords(refused[0].st.refreshRefused);
                        win.checkedEl.textContent += refused.length > 1
                            ? (' — ' + refused.length + ' brokers did not re-ask')
                            : (' — ' + w);
                    }
                    win.checkedEl.title = refused.length
                        ? refused.map(r => (r.label || 'this broker') + ': '
                              + refreshRefusedWords(r.st.refreshRefused)).join('\n')
                        : (rows.length > 1
                            ? 'the oldest of these checks — each broker is asked, '
                                + 'and answers, separately'
                            : '');
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

                    // ---- after an apply (#182 Part 2, A29) ----
                    // Above the restart control on purpose: a deploy that
                    // failed, rolled back, or left survivors on old code is
                    // the loudest fact in this window, and it must not sit
                    // below the fold of a list of healthy rows. A broker
                    // with no deploy history renders nothing here at all.
                    renderDeployStrip(body);

                    // ---- restart THIS broker (#183) ----
                    // Placed here, beside the shared facts about the local
                    // build that motivate it, and NOT inside the per-broker
                    // list below: the window names every configured host,
                    // but this control is about exactly one of them,
                    // always, regardless of which rows follow.
                    addHead(body, 'Restart');
                    renderRestartRow(body);
                    // ---- apply an update THIS broker (#182 Part 2, A30) ----
                    // Same section as the restart control above: an apply
                    // ENDS in exactly that restart, and the row it renders
                    // is LOCAL ONLY for the same reason (apply never touches
                    // a remote host).
                    renderApplyRow(body);

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
                        // #182: the switch belongs to the ROW, not to a section
                        // of its own. It used to sit above, about the serving
                        // broker only, which made the one broker an operator
                        // most needs to reach — a remote one they have no local
                        // session on — the only broker they could not switch on.
                        // Rendered only where there is something to DO, so a
                        // fleet that is already checking reads as a plain list.
                        if (policyRowNeeded(r.id)) {
                            renderPolicyRow(body, r.id, r.label);
                        }
                        // A6: the self-update grant row, right below the
                        // checking switch it generalizes.
                        if (selfUpdateRowNeeded(r.id)) {
                            renderSelfUpdateRow(body, r.id, r.label);
                        }
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
                    applyOpDead = true;
                    stop();
                    for (const w of Array.from(windows.values())) {
                        if (w && w.type === 'app' && w.appKind === 'update') {
                            closeWindow(w.id);
                        }
                    }
                });

                renderChip();
                start();
                // #182: the consent attempt goes FIRST when there is one, so a
                // freshly-ticked mod resolves to an answer rather than flashing
                // "switched off here" and correcting itself a moment later. It
                // polls the local broker itself on success; the fleet-wide tick
                // follows either way, and .catch because nobody awaits this —
                // an unguarded rejection would surface on the page instead of
                // in this mod.
                if (ctx.enabledByUser) {
                    offerConsent()
                        .catch(function () {})
                        .then(function () { pollTick(); });
                } else {
                    pollTick();
                }
            },
        });
