        // ---- mod boot (#71) -----------------------------------------------
        // Ordered LAST among the JS fragments: every mod script above has run
        // its registerMod() by now, so loadMods() gates on mods_enabled and
        // inits them all (each isolated). Async + fire-and-forget — boot does
        // not block on the /info round-trip.
        // #167: ...and this is the ONLY place that knows when that round-trip is
        // over, so it is also where the second app-window restore pass hangs.
        // A persisted mod-owned window (file-manager / scratchpad) is dropped for
        // the whole page load when restore wins the race against the kinds this
        // registers; restoreAppWindowsAfterMods (84) re-attempts exactly the
        // records that race skipped, self-guarded. Hooked HERE rather than inside
        // loadMods so it
        // survives every loader outcome: the mods_enabled=false early return, an
        // unexpected throw, and the ordinary success path all land in one of the
        // two handlers below. A mods-off browser losing restore entirely would be
        // a strictly worse bug than the one being fixed.
        loadMods().then(
            function () { restoreAppWindowsAfterMods(); },
            function (e) {
                console.error('[mods] loadMods failed', e);
                restoreAppWindowsAfterMods();
            }
        ).catch(function (e) {
            // The reject handler above runs the retry too, so a throw from IT would
            // otherwise surface only as an unhandled rejection.
            console.error('[mods] post-load app window restore failed', e);
        });
