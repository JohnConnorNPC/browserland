        // ---- mod boot (#71) -----------------------------------------------
        // Ordered LAST among the JS fragments: every mod script above has run
        // its registerMod() by now, so loadMods() gates on mods_enabled and
        // inits them all (each isolated). Async + fire-and-forget — boot does
        // not block on the /info round-trip.
        loadMods();
