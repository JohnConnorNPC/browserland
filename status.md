# Loop status — FINAL (feature loop complete)

**🏁 ALL ACTIONABLE FEATURES DONE.** No unchecked, unblocked items remain → FINISH reached.

## Tally
- **81 / 81 actionable features done** (F001–F081, all `[x]`).
- **1 blocked** (await a human decision; never auto-picked):
  - **F082 — Untab: drag a tab out of the strip.** ✅ RESOLVED by #61 (no longer blocked). The owner chose the doc fix: the stale "Drag a tab out of the tab strip to give it its own tile again" claim was removed from `wiki/Arranging-Windows.md` and `help_corpus.json` was regenerated. The gesture was never implemented; the other 3 untab mechanisms — menu split-to-rows, side-by-side, ⊟ button — DO work (verified under F031). Docs now match behavior.
  - **F083 — Control Panel full-screen presentation.** DOC/BEHAVIOR gap split from F060. Wiki `Getting-Started.md:42` says the panel "opens as a full-screen panel", but the code renders `#settings-modal` capped ≤480×80vh (centered dialog; only the dimmed backdrop is full-screen). Functional panel (per-broker tabs + all sections + per-host/global routing) verified done under F060. Resolve by EITHER enlarging `#settings-modal` toward full-screen (index.html edit, deferred) OR correcting the wiki wording. Owner call on intended UX.

## Final full-suite run (FINISH step)
- `cd /x/Data/browserland && python -m pytest -q` → **449 passed, 11 skipped, 0 failed** (11.13s). No feature regressed; nothing unchecked.

## Verification notes (how each block was confirmed)
- **Backend (.py)** features (protocol/agent/PTY/broker/launcher/MCP F065-F076,F080,F081, snapshot F070-F073) — EXECUTED pytest (tests/test_{mcptool,integration,protocol,registry_agent,textgrid,pyte_snap,snapshot_raw,altscreen,broker_e2e}.py) + throwaway Sanic ASGI probes for broker /mcp/* gate behavior not covered by committed tests.
- **Frontend (index.html, F023-F062)** — INSPECTION-ONLY with file:line evidence (no headless browser stood up; index.html left untouched due to the pre-existing uncommitted change). Lower-confidence than executed tests; offered Playwright UI checks if desired.
- **Code-wins corrections** (feature line/expectation was stale, code is intended + test/UI-pinned): F018 (CORS `*` by design), F022 (/file/* full-host auth not sandbox), F069 (GET /mcp/config returns token by design — UI copies it), F076 (8 tools not 7 — reset_terminal #27 test-pinned), F080 (machine_host named at MCP-tool layer).

## Recurring test-hardening opportunity (NOT a feature gap)
Broker-side `/mcp/*` + `/session/mcp` endpoints (F066/F067/F068/F069/F074 gates: effective-mode, robot override, allow_launch, config admin, input verbatim/256KiB/lease-bypass) are CORRECT but have NO committed broker-side tests — coverage is client-side in test_mcptool.py; each was proven via throwaway ASGI scripts (not committed). A permanent `tests/test_mcp_broker.py` (Sanic ASGI-driven) would close this. Offered to owner.

## Stop condition
Per feature_loop.md FINISH: full suite passed, everything `[x]` except the 2 documented Blocked items. **Loop stops here.** Re-running the loop prompt will find no actionable item and re-confirm this state. To proceed on F082/F083, the owner picks a direction (and lifts the index.html-edit hold from the pre-existing uncommitted change).
