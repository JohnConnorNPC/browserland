        // ---- ctx.hosts.list(): VERIFIED broker identity for mods (#200) ------
        //
        //     ctx.hosts.list();
        //     // -> [{id, label, url, brokerId, self}]   read-only snapshot
        //
        // WHAT IT REPLACES. Mods had no stable host identity, so update
        // invented one: `hostFingerprint = host.url`
        // (mods/update/update.js:925-928). A URL is not an identity — it is an
        // ADDRESS. The same broker reached as 127.0.0.1, localhost and the
        // Tailscale 100.x address is three fingerprints; a URL edited to point
        // at a DIFFERENT broker keeps the same fingerprint if the string is
        // unchanged in the caller's cache, and keeps the old broker's cached
        // state either way. #64 already put the real answer on the host record:
        // `brokerId`, the broker's own `broker_id` out of /info, learned by
        // probeBrokerId / learnLocalBrokerId / refreshHostIdentities (83). This
        // fragment EXPOSES that, it does not mint anything.
        //
        // IDENTITY IS TWO FIELDS, and neither one pretends to be the other:
        //
        //   `id`       — the local registry handle (getHosts, 56). Stable for
        //                THIS browser's list and meaningless anywhere else:
        //                'local' here and 'local' in another browser are two
        //                different brokers, and the same broker carries a
        //                different minted id in every browser that added it.
        //                Use it to address a host through the rest of the ctx.
        //
        //   `brokerId` — cross-URL identity: the broker SAID this, over /info,
        //                and it is the same string under every address that
        //                broker answers on. Use it as a cache key, or to notice
        //                that two rows are one machine.
        //
        // `brokerId` IS null UNTIL THE HOST HAS ANSWERED. Not a provisional
        // hash of the URL, not the URL itself, not the id — null. A guess that
        // is later silently replaced by a verified value is worse than an
        // absence: every cache keyed on the guess survives the swap, pointing
        // at the wrong broker, and nothing in the data says which entries are
        // verified. `null` is a value a caller can branch on, and the branch is
        // the point: "unverified" is a real state (an older broker with no
        // /info, a wrong token, a host that is simply offline), it can last for
        // the whole session, and a caller must decide what it means rather than
        // fall back to the URL and re-create the bug.
        //
        // A URL REPOINT RESETS IT TO null, and it is NEVER carried over. #200
        // is explicit: carrying it would label a new — possibly hostile —
        // endpoint with the old broker's verified identity, which is the exact
        // shape of #174's host-repoint vector one layer up. Core's edit path
        // (commitHostForm, 83) now clears `brokerId` when the URL actually
        // changes, and the repaint that follows re-runs refreshHostIdentities,
        // so the field goes null and then either comes back the same (the same
        // broker under a second address) or comes back DIFFERENT (a genuinely
        // different broker) — which is the transition URL-as-fingerprint could
        // not represent at all.
        //
        // NEITHER FIELD IS EVER A MERGE KEY FOR REGISTRY ENTRIES. That is
        // #174's lesson, and it cost a host-repoint vector to learn: an
        // INCOMING entry's `id` was matched against a local record and the
        // local record's URL was then updated from the incoming one, so anyone
        // who could write the shared blob could silently re-point a broker
        // entry at a machine they controlled. `brokerId` is no safer for that
        // job — it is a string a remote endpoint chose to say. Both fields are
        // for CHANGE DETECTION and CACHE KEYS on this page only. Nothing here
        // merges anything, and a mod that syncs host records must key its merge
        // on something the user authorized, never on either of these.
        //
        // EXACTLY ONE ENTRY HAS self:true — the local broker, getHosts()'s
        // entry 0 (id 'local', url ''). Its `brokerId` is learned by a
        // DIFFERENT path from every other entry (learnLocalBrokerId, off the
        // memoized same-origin localInfo()), which is exactly why it is not
        // special-cased here: same field, same null-until-answered rule, so a
        // caller needs no branch for "is this me" beyond the flag itself.
        //
        // READ-ONLY SNAPSHOT. Every entry is a fresh frozen object of copied
        // scalars and the array is frozen too, so a mod cannot reach the live
        // prefs._hosts record through it: no write lands, and no later mutation
        // of the registry mutates a snapshot a mod is holding. Host records are
        // written by core (the Hosts form, the hide toggle, the colour swatch)
        // and forgotten through ctx.hosts.invalidate; there is deliberately no
        // ctx-side mutation door here.
        //
        // NO `token`. The host record carries the broker's auth password. It is
        // not identity, no mod needs it to key a cache, and handing it out
        // would make every mod a credential holder — ctx.serverStore / hostFetch
        // already carry the token for the mod, without showing it.
        //
        // NO CAPABILITY ENTRY OF ITS OWN, and the argument matters because the
        // drift gate reads it both ways. #197's map is per FAMILY, and `hosts`
        // is an EXISTING family: #195 created it in 86c for `invalidate` and
        // registered `hosts` there. `list` is a MEMBER of it — the saveChain
        // case (a member of `serverStore`, which owed nothing), not the
        // signal/hosts case (a new top-level family, which owed an entry).
        // `needs: ['hosts.list']` resolves against the member by path, and a
        // mod feature-detects with `typeof ctx.hosts.list === 'function'`.
        // The guarded _registerModCapability call below is therefore NOT a
        // second entry: it is idempotent (86c's registrar refuses a name
        // already present) and exists only for the build where 86c installed no
        // `hosts` family at all — its extender bails when core's invalidateHost
        // is missing — because THIS fragment would then be the one putting
        // `hosts` on the ctx, and a family on the ctx with no entry makes
        // ctx.capabilities lie by omission. ctxVersion stays 1.
        //
        // ITS OWN FRAGMENT, for the reason 86e/86f/86g/86h each got one:
        // 86c_js_mod_ctx_ext.js is at the #68 2500-line per-fragment cap and
        // the rule for that cap has always been split, never trim. Ordered
        // after 86c (extenders run in _ORDERED order, and this one DECORATES
        // the family that fragment created) and still before the mod-script
        // splice, since a mod's init reads the finished ctx. One <script>, one
        // scope, calling 86c's _registerCtxExtender the same typeof-guarded way
        // every companion does.

        // '' is how the host record spells "not learned yet" (83 writes '' on a
        // repoint; a record added before /info existed simply has no field).
        // The CONTRACT is null, in one place, so no caller has to know that.
        function _modHostBrokerId(h) {
            const v = h && h.brokerId;
            return (typeof v === 'string' && v) ? v : null;
        }
        // One frozen row of COPIED scalars — never the live record.
        function _modHostEntry(h, selfId) {
            return Object.freeze({
                id: h.id,
                label: (typeof h.label === 'string') ? h.label : '',
                url: (typeof h.url === 'string') ? h.url : '',
                brokerId: _modHostBrokerId(h),
                self: h.id === selfId,
            });
        }
        function _modHostsList() {
            let hosts = null;
            try { hosts = getHosts(); }
            catch (e) {
                try { console.error('[mods] hosts.list failed:', e); }
                catch (_) { /* console is gone */ }
                return Object.freeze([]);
            }
            if (!Array.isArray(hosts)) return Object.freeze([]);
            // getHosts() guarantees entry 0 is the implicit local host (id
            // 'local'), rebuilt on every call — so "exactly one self" is read
            // off the SAME array the rows come from rather than assumed. The
            // positional fallback keeps the promise even if a future core
            // change renames that id.
            let selfId = null;
            for (const h of hosts) {
                if (h && h.id === 'local') { selfId = h.id; break; }
            }
            if (selfId === null && hosts.length && hosts[0]) {
                selfId = hosts[0].id;
            }
            const out = [];
            for (const h of hosts) {
                if (!h || typeof h.id !== 'string' || !h.id) continue;
                out.push(_modHostEntry(h, selfId));
            }
            return Object.freeze(out);
        }
        function _ctxHostsList(ctx) {
            if (typeof getHosts !== 'function') return;
            // DECORATES: 86c's _ctxHosts put `invalidate` on this family and a
            // later fragment may add more. Replacing the object would delete a
            // sibling's members — the rule every window extender follows.
            const fam = (ctx.hosts && typeof ctx.hosts === 'object')
                ? ctx.hosts : {};
            fam.list = function () { return _modHostsList(); };
            ctx.hosts = fam;
        }
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxHostsList);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('hosts', 1);
        }
        // ---- end ctx.hosts.list ---------------------------------------------
