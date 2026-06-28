# Loop status — handoff to next iteration

**Just handled:** F026 — Column width presets → **done** (FRONTEND inspection-verified, index.html not edited). `WIDTH_PRESETS=['1/3','1/2','2/3','max']` (idx:2947); `presetFraction()` maps 1/3→.333,1/2→.5,2/3→.667,max→1 (2948-2956); `PRESET_LABELS` ⅓/½/⅔/max (13841-13842); applied via `presetFraction(col.widthPreset)` (5097,5305). `setColumnPreset(win,preset)` (13843-13852): validate, set `loc.col.widthPreset`, delete custom `widthFrac` drag override, `savePrefs()`+`requestRelayout()`. Menu: `buildWindowMenu` (14151, gated `win.tiled` 14153) "Column width" header (14161) + per-preset items (14162-14168)→`setColumnPreset`. ✓ marker: `cur=loc.col.widthPreset` (14155), label prefix `(cur===p?'✓ ':' ')` (14164). No gap, no code change.

**Next to pick:** F027 — Column gutter drag-resize (index.html): drag the seam between columns for a custom width. FRONTEND inspect-only.

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 = index.html):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing change) or run pytest for pure-frontend; real gap → report as finding (don't fix). Only commit every_feature.md + status.md.

**Reminder:** already-built — VERIFY, don't rebuild. Docs/spec vs code disagree → code usually wins (F018/F022), correct the feature line. F045 closed-notes dep F050's + menu; F058 help-MCP-status dep F065 — skip-and-return if before deps. Backend (.py) CAN be patched + pytest-verified. Pre-existing index.html change — leave alone.
