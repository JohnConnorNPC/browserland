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
                    'disabled': 'update checking is switched off on this '
                        + 'broker. An operator enables it with '
                        + '"update_check_enabled" in the broker config',
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
                // The host the chip and the window currently describe. Held as
                // an id, not as a record: the record is looked up on every
                // render so it can never be read out from under the host it
                // describes.
                function viewRecord() { return checkStateFor(LOCAL_HOST_ID); }
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
                    return 'grey';   // ahead-or-diverged and unknown both
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

                function renderChip() {
                    const st = viewRecord();
                    const s = state(st);
                    // "Quiet when current" hides only the CURRENT state, never
                    // unknown: an unknown that hid itself would be
                    // indistinguishable from a green one, which is exactly the
                    // confusion this mod exists to prevent.
                    const quiet = quietSetting.get() && s === 'current';
                    chip.style.display = quiet ? 'none' : 'inline-flex';
                    const band = BANDS[bandFor(s)];
                    chip.style.borderColor = band.border;
                    chip.style.color = band.fg;
                    chipText.textContent = chipLabel(st, s);
                    chip.title = chipTitle(st);
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
                // one is a REASONS key above so the window can say it:
                //   'ready'         ask it — the route is there
                //   'disabled'      the route is there and that operator never
                //                   opted in. /info already said so, so the
                //                   request is skipped rather than spent
                //                   earning a 503 we can already predict
                //   'route-absent'  it predates the route. NOT asked.
                //   'unauthorized'  it refused our password
                //   'unreachable'   nothing came back from it at all
                //
                // NOTE (seam): layer 1 below reads `rec.update`, the capability
                // /info now publishes. fetchModCatalog does not yet copy that
                // key onto the record it caches, so until it does this layer is
                // inert and the probe falls through to layers 2-4. That is safe
                // — those layers never ask a peer that lacks the route — it only
                // costs the shortcut that would let a `check_enabled:false`
                // broker be answered without a round trip.
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
                                ? 'disabled' : 'ready';
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
                        if (rec.state === 'headless') return 'ready';
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
                        if (cap !== 'ready') {
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
                            if (r.status === 503) {
                                // The broker's own gate. A capability that is
                                // absent here, NOT an error and NOT "up to
                                // date". Still reachable after the probe: an
                                // older peer can have the route while publishing
                                // no `update` key on /info, and then the 503 is
                                // the only authority on the gate. Same wording
                                // either way, so an operator cannot tell (and
                                // does not need to) which of the two said it.
                                st.check = null;
                                st.error = 'disabled';
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

                // The driver. Still LOCAL-ONLY: the state below it is per host,
                // but who gets polled is deliberately unchanged here, so this
                // change cannot alter what the current single-broker chip says.
                // Fanning out is a loop over the host list, not a rewrite.
                function pollTick() { poll(LOCAL_HOST_ID); }
                // The deliberate retry. The capability probe caches its outcome
                // — including its failures — precisely so a tick can never
                // re-ask a broker that is asleep or too old, which leaves
                // exactly one way for that verdict to ever change: somebody
                // asking for it. "Check now" is that somebody, so it re-reads
                // /info as well as re-asking for the version. Without this, a
                // broker updated while the tab was open would stay 'route-absent'
                // until the page was reloaded.
                function recheck() { poll(LOCAL_HOST_ID, { refresh: true }); }

                function start() {
                    stop();
                    timer = setInterval(pollTick, POLL_MS);
                }
                function stop() {
                    if (timer) { clearInterval(timer); timer = null; }
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
                    refreshBtn.title = 're-ask the broker (it caches for a day)';
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
                // here except our own literals came off the network.
                function addRow(body, label, value, cls) {
                    const row = document.createElement('div');
                    row.className = 'app-upd-row';
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

                // Idempotent: rebuild from the record every call. The window
                // still shows the LOCAL broker's record — one host per window
                // is the shape it has today, and picking a host is a later
                // change; what matters here is that it reads a record it names,
                // not whatever the module last happened to see.
                function renderWindow(win) {
                    if (!win || win.disposed) return;
                    const st = viewRecord();
                    const chk = st.check;
                    if (win.checkedEl) {
                        let t = '';
                        if (st.checkedAt) {
                            try {
                                t = new Date(st.checkedAt).toLocaleTimeString();
                            } catch (_) {}
                        }
                        // checkedAt 0 = no poll has COMPLETED for this host yet,
                        // so it says "checking…" rather than dating an answer
                        // that was never established.
                        win.checkedEl.textContent = st.checkedAt
                            ? ('checked ' + t) : 'checking…';
                    }
                    const body = win.body;
                    body.innerHTML = '';
                    const s = state(st);

                    const headline = addRow(body, 'Status', ({
                        'current': 'up to date',
                        'behind': 'a newer build is available',
                        'ahead-or-diverged': 'this checkout is ahead of upstream',
                        'unknown': 'could not be established',
                    })[s] || 'could not be established', 'app-upd-' + bandFor(s));
                    headline.classList.add('app-upd-headline');

                    const local = (chk && chk.local) || {};
                    addRow(body, 'This build', local.version
                        ? (local.version + (local.sha
                            ? ('  (' + String(local.sha).slice(0, 10) + ')') : ''))
                        : 'unknown');

                    const up = chk && chk.upstream;
                    if (up) {
                        addRow(body, 'Upstream', up.tag
                            || (up.sha ? String(up.sha).slice(0, 10) : '—')
                            + (up.branch ? ('  on ' + up.branch) : ''));
                    }
                    if (chk && chk.repo) addRow(body, 'Tracking', chk.repo);

                    if (s === 'behind' && typeof chk.behindBy === 'number') {
                        addRow(body, 'Behind by', chk.behindBy + ' commit'
                            + (chk.behindBy === 1 ? '' : 's'));
                    }
                    if (s === 'ahead-or-diverged') {
                        const a = chk && chk.aheadBy;
                        const b = chk && chk.behindBy;
                        addRow(body, 'Ahead by', (a || 0) + ' commit'
                            + (a === 1 ? '' : 's')
                            + (b ? (', behind by ' + b) : ''));
                        addNote(body, 'A checkout with commits upstream has '
                            + 'never seen is a development checkout, not a '
                            + 'stale one.');
                    }

                    if (s === 'unknown') {
                        const code = reasonCode(st);
                        addNote(body, 'Why: ' + (REASONS[code]
                            || 'the reason was not reported') + '.',
                            'app-upd-why');
                    }

                    // The link out. Built from our own constants plus the sha —
                    // href is assigned, never innerHTML'd.
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

                    if (s === 'behind') {
                        addNote(body, 'To update: stop the broker, run '
                            + '"git pull --ff-only" in the checkout, reinstall '
                            + 'dependencies if pyproject.toml changed, then '
                            + 'start it again and reload this page.',
                            'app-upd-howto');
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
