        // ---- startup --------------------------------------------------------
        if (isSizeLocked()) document.body.classList.add('size-locked');
        applyDisplaySettings();
        // One-time tiling-by-default migration: the reconcileLayout default
        // only catches an invalid/missing mode, so an existing saved
        // 'floating' layout would not flip. Flip it exactly once here; after
        // that the Control Panel toggle is fully in control (a later floating
        // choice sticks because the flag is already set).
        (function applyTilingDefault() {
            const s = getSettings();
            if (s.tilingDefaultApplied) return;
            s.tilingDefaultApplied = true;
            if (!isTilingMode()) enterTilingMode();   // flips live + re-tiles + saves
            savePrefs();
        })();
        renderWorkspaces();          // pager/rail visible from the first paint
        // Open the HOME control WS FIRST: the broker auto-activates a lone
        // browser (sends {active:true} -> bootActiveView), or reports
        // {active:false} so we show only the "Become active" button. The heavy
        // restore is deferred to bootActiveView so an inactive browser never
        // builds windows (near-zero flash).
        openControlWs(localHost());
        // Footgun guard: if the HOME control WS never reports a status (a
        // blocked WS upgrade, or a broker missing the /control route) the
        // deferred boot would leave a blank page forever. After a grace period
        // with no HOME status, surface the overlay so there is at least a
        // visible "Become active" affordance — the control WS keeps retrying in
        // the background and a real status then drives the normal transition.
        setTimeout(() => {
            if (!_booted && !_deactivated) showBecomeActiveOverlay();
        }, 8000);
        // Initial shared-state adopt — read-only and lease-independent (the
        // push is gated by _deactivated), so a reactivating tab and the active
        // one share rev. Resolves _stateReadyPromise to release bootActiveView.
        (async function initStateSync() {
            await pullState(true);
            _stateReady = true;
            if (_statePendingPush) { _statePendingPush = false; schedulePush(); }
            _markStateReady();
        })();
        fetchProfiles(localHost());   // prefetch LOCAL only — remote menus
                                      // populate lazily on right-click
        learnLocalBrokerId();         // #64: same-origin id for dup detection
