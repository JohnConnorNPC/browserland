# Loop status — handoff to next iteration

**Just handled:** F025 — Toggle tiling mode → **done** (FRONTEND inspection-verified, index.html not edited). `enterTilingMode()` (idx:13862-13868: set `mode='tiling'`+`savePrefs()`+`attachToStrip` floats→strip+`renderWorkspaces`+`requestRelayout`); `enterFloatingMode()` (13872-13880: `detachToFloat`). `toggle-tiling` action (13971-13973: isTilingMode()?float:tile). Ctrl+Alt+t: `DEFAULT_KEYBINDINGS['toggle-tiling']='Ctrl+Alt+t'` (2577) → capture keydown dispatcher (14044-14058) comboFromEvent→KEY_ACTION_BY_ID→act.run(). `#set-tiling` checkbox (1789), getElementById 14516, change handler 15590-15614 (local→enter*; remote→`entry.layout.mode` 15611), synced on panel open 15318-15320. Per-host: local `prefs._layout.mode` (getLayout 3608-3610, synced slice 2098/2177); remote `hostStateCache.get(hostId).layout.mode` via `remoteTilingMode` (15500-15504) — not browser-global. No gap, no code change.

**Next to pick:** F026 — Column width presets (index.html): ⅓ / ½ / ⅔ / max from title-bar menu, current marked with ✓. FRONTEND inspect-only.

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 = index.html):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing change) or run pytest for pure-frontend; real gap → report as finding (don't fix). Only commit every_feature.md + status.md.

**Reminder:** already-built — VERIFY, don't rebuild. Docs/spec vs code disagree → code usually wins (F018/F022), correct the feature line. F045 closed-notes dep F050's + menu; F058 help-MCP-status dep F065 — skip-and-return if before deps. Backend (.py) CAN be patched + pytest-verified. Pre-existing index.html change — leave alone.
