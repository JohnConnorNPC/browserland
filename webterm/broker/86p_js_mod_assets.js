        // ---- ctx.assets.url (#202) ------------------------------------------
        //
        //     const u = ctx.assets.url('sprites.css');
        //     // installed mod: '/mods/<id>/<gen>/sprites.css'
        //     // shipped mod:   undefined   -- feature-detect the RETURN VALUE
        //
        // An INSTALLED mod already has a whole content-addressed serving
        // pipeline behind it -- GET /mods/<id>/<gen>/<name> (#163), which 86b
        // uses to inject that package's own scripts and styles -- and until now
        // no handle on its own bytes. A mod that wants to fetch a file it
        // shipped (a data table, a stylesheet it injects itself, a worker
        // source) had to hand-build that path out of window.__mods, which is
        // the loader's mutable fixture and not a contract. This is the handle.
        //
        // THE DECISIONS THE ISSUE MADE, implemented here rather than rediscussed:
        //
        //  1. IT RIDES THE EXISTING GENERATION MACHINERY, AND NOTHING ELSE.
        //     The gen comes from window.__mods.packages[<id>].gen -- the
        //     per-page load record 86b's _modPackage() builds out of the boot
        //     catalog row (/info's `mods`) -- and the URL is built by 86b's own
        //     _modAssetUrl(id, gen, name), the same builder the <script src>
        //     injection uses. There is deliberately no second source of truth
        //     for a generation: if a mod's asset URL and its script URL could
        //     ever disagree, one of them would be serving bytes nobody
        //     reviewed under a hash that says otherwise.
        //
        //  2. A SHIPPED MOD GETS `undefined`, ON PURPOSE. A shipped mod is
        //     spliced into the one inline <script> at assembly time; its files
        //     live in the shipped tree, which no route serves per-file. Making
        //     one would extend FORCED-PUBLIC serving to the shipped tree --
        //     a new exposure decision that gets its own issue, not a side
        //     effect of this one. So: no /mod-assets/<id>/ route here, and no
        //     faking it with a relative path either. A relative path would
        //     "work" in a hand test and 404 in the page, which is worse than
        //     the honest `undefined` a mod can feature-detect.
        //
        //  3. RESOLVE PER PAGE LOAD. NEVER PERSIST A URL. The URL is
        //     content-addressed and served `immutable, max-age=31536000`: the
        //     <gen> segment IS the package's content hash, so an upgrade
        //     publishes a DIFFERENT URL and the old one stops resolving once
        //     that generation is reaped. A mod that stashes one in ctx.prefs,
        //     ctx.storage or /state gets a URL that keeps working right up
        //     until the day the mod is updated, and then 404s with no error
        //     anyone can attribute. Call url() at the point of use, every
        //     time. It is a property read and a string concat; there is
        //     nothing to memoize.
        //
        //  4. THE ROUTE IS FORCED-PUBLIC. NO SECRETS IN ASSETS, EVER. It has
        //     to be: a <script src> cannot carry an Authorization header, so
        //     /mods/<id>/<gen>/<name> serves without a token by design. Every
        //     byte a mod ships is readable by anyone who can reach this broker
        //     and knows the id, the gen and the file name. Ship code and
        //     static data there; never a credential, a host list or anything
        //     derived from one.
        //
        // THE STANDING QUESTION -- what can a mod ask for that the route will
        // NOT serve? -- answered case by case, because the answer has to be a
        // line of code and not a hope. The rule below is MEMBERSHIP, not
        // sanitisation: a name is answered only when it is a key of the
        // package's own `integrity` map, which the broker builds from the
        // files of that generation filtered by content_type (modinstall._row).
        // That set IS the set of names the route will serve, so an unservable
        // name is unrepresentable here rather than defended against:
        //
        //   * '../../etc/passwd', 'a/b.js', '/mods/x/y/z.js' -- traversal and
        //     any embedded slash. Not a key of the map, so `undefined`. The
        //     grammar check below refuses them a second time, before the map
        //     is consulted at all, because THIS is the case that matters: the
        //     route is forced-public, so a URL that looks plausible but
        //     escapes the package directory is the one worth two refusals.
        //     (Sanic's <name:str> matches ONE segment and the route re-runs
        //     modinstall.file_name_error anyway -- three layers, none of them
        //     load-bearing alone.)
        //   * A LEADING SLASH ('/x.js') -- refused by the grammar (a name must
        //     start [A-Za-z0-9]) and absent from the map. `undefined`.
        //   * A NAME NOT IN THE PACKAGE -- absent from the map. `undefined`,
        //     never a URL that 404s later: a mod can branch on the answer.
        //   * AN EMPTY NAME, A NON-STRING (null, a number, an object with a
        //     toString) -- `undefined`, with no coercion. A name is a string
        //     or it is nothing; coercing would make `url({})` build
        //     '/mods/x/<gen>/%5Bobject%20Object%5D'.
        //   * A QUERY STRING OR FRAGMENT ('a.js?v=2', 'a.js#top') -- '?' and
        //     '#' are outside the grammar and outside the map. `undefined`.
        //     They would otherwise let a name express its own URL structure.
        //   * A NAME THE PACKAGE SHIPS BUT THE ROUTE CANNOT SERVE (help.md,
        //     mod.json) -- `integrity` already omits it (content_type returns
        //     None for .md, and the manifest name is reserved), and the suffix
        //     check below refuses it too. `undefined`.
        //   * A MOD WHOSE PACKAGE WAS UNINSTALLED OR UPGRADED MID-SESSION --
        //     answered from THIS page's load record, which is frozen at boot,
        //     so url() keeps returning the generation this page actually
        //     loaded. That is the correct answer, not a stale one: the running
        //     code IS that generation, and #163's contract is that an
        //     install/uninstall/replace applies on the NEXT page load. The
        //     bytes may nevertheless be gone from the store (an uninstall
        //     reaps them), so a fetch of the returned URL can 404 at any time
        //     -- which is exactly why decision 3 forbids persisting one.
        //   * A PACKAGE WITH NO GENERATION -- a catalog row whose `gen` was
        //     missing or malformed leaves _modPackage's default ''. Refused by
        //     the 64-hex check: `undefined`, never '/mods/x//a.js'.
        //   * A MOD WHOSE ID DOES NOT MATCH ITS PACKAGE -- can't arise; the
        //     lookup is keyed by the ctx's OWN id, so a mod can only ever ask
        //     about its own package. There is deliberately no url(modId, name)
        //     form: one mod addressing another's assets is a different feature
        //     with a different exposure argument.
        //
        // ITS OWN FRAGMENT, for the reason 86e-86n each got one: 86c is at the
        // #68 2500-line per-fragment cap (_MAX_LINES, ui.py) and the rule for
        // that cap has always been "split, never trim". Ordered AFTER 86c (it
        // uses that fragment's extender registry and capability map, and
        // extenders run in ui._ORDERED order) and after 86b, whose
        // _modAssetUrl and package bag it reads. It shares nothing with 86m /
        // 86n, so its order among them is free. Still before the mod-script
        // splice, since a mod's init reads the finished ctx.

        //: The name grammar, mirroring modinstall._FILE_NAME_RE. Belt to the
        //: membership braces below: the map's keys come from the broker and are
        //: already legal, so this exists so that a name can NEVER express a
        //: path, a query, a fragment or an NTFS stream even if that map were
        //: ever poisoned or relaxed.
        const _MOD_ASSET_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
        //: Only these are servable at all (modinstall.CONTENT_TYPES). `.md` is
        //: a broker-side INPUT (Help), never a URL.
        const _MOD_ASSET_SUFFIXES = ['.js', '.css'];
        //: A gen is a sha256 hex digest and nothing else (modinstall.GEN_RE).
        const _MOD_ASSET_GEN_RE = /^[0-9a-f]{64}$/;

        // This page's load record for `modId`, or null. Shipped mods have none
        // -- they were never fetched from the store -- which is what makes
        // decision 2 a lookup miss rather than a special case.
        function _modAssetsPackage(modId) {
            if (typeof modId !== 'string' || !modId) return null;
            const mods = window.__mods;
            const bag = (mods && mods.packages) || null;
            if (!bag || typeof bag !== 'object') return null;
            // hasOwnProperty by call, not by method: the bag is
            // Object.create(null) (86b) and has no prototype to reach it on.
            if (!Object.prototype.hasOwnProperty.call(bag, modId)) return null;
            const pkg = bag[modId];
            return (pkg && typeof pkg === 'object') ? pkg : null;
        }

        // The whole answer. `undefined` for every refusal, uniformly: a mod
        // feature-detects the RETURN VALUE, and a throw here would cost an
        // init over a typo'd file name.
        function _modAssetsUrl(modId, name) {
            if (typeof name !== 'string' || !name) return undefined;
            if (!_MOD_ASSET_NAME_RE.test(name)) return undefined;
            const lowered = name.toLowerCase();
            let servable = false;
            for (const sfx of _MOD_ASSET_SUFFIXES) {
                if (lowered.length > sfx.length
                        && lowered.slice(-sfx.length) === sfx) servable = true;
            }
            if (!servable) return undefined;
            const pkg = _modAssetsPackage(modId);
            if (!pkg) return undefined;                    // shipped, or unknown
            const gen = pkg.gen;
            if (typeof gen !== 'string' || !_MOD_ASSET_GEN_RE.test(gen)) {
                return undefined;
            }
            const integrity = pkg.integrity;
            if (!integrity || typeof integrity !== 'object') return undefined;
            // MEMBERSHIP is the real check: `integrity` is keyed by exactly the
            // files of this generation the route can serve.
            if (!Object.prototype.hasOwnProperty.call(integrity, name)) {
                return undefined;
            }
            // 86b's builder, never a hand-rolled concat: one URL shape for the
            // page, encodeURIComponent'd per segment there.
            if (typeof _modAssetUrl !== 'function') return undefined;
            return _modAssetUrl(modId, gen, name);
        }

        // The extender. `assets` is a family EVERY mod gets, shipped or
        // installed -- there is no half-family here. The per-mod difference
        // lives in the RETURN VALUE (decision 2), which is the only place a
        // mod can feature-detect it without also having to feature-detect the
        // build. A missing `ctx.assets` on a shipped mod would instead mean
        // every caller writes `ctx.assets && ctx.assets.url(...)` AND checks
        // the result, i.e. two detections for one fact.
        function _ctxModAssets(ctx) {
            const modId = ctx && ctx.id;
            ctx.assets = Object.freeze({
                // NEVER cache what this returns across a page load: the <gen>
                // segment is the package's content hash, so an upgrade changes
                // it (decision 3).
                url: function (name) { return _modAssetsUrl(modId, name); },
            });
        }
        // Guarded like every other registration in an extension fragment: a
        // page assembled without #194's registry or #197's capability map must
        // not throw at load. The capability entry is what keeps
        // ctx.capabilities a TRUE inventory -- a family an extender adds with
        // no entry would make the map lie by omission.
        if (typeof _registerCtxExtender === 'function') {
            _registerCtxExtender(_ctxModAssets);
        }
        if (typeof _registerModCapability === 'function') {
            _registerModCapability('assets', 1);
        }
        // ---- end ctx.assets.url ---------------------------------------------
