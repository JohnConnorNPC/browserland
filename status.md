# Loop status — handoff to next iteration

**Just handled:** F029 — Eject column → **done** (FRONTEND inspection-verified, index.html not edited). "Move to own column" menu item (idx:14177-14179)→`expelToNewColumn(win)` (3758-3777): `removeKeyFromLayout` (3767) + `newColumn()` (3768-70) spliced after src col (insertAt=indexOf(srcCol)+1, 3771-72). "Move to new column" item (14183-14185)→`dragDropNewColumn(win.id, loc.colIndex+1)` (3907-3935): remove key (3920), new single-win col `ws.columns.splice(idx,0,col)` (3927-30), no-op guard when alone (3914-19), slot-shift fix on collapse (3923-25). `removeKeyFromLayout` (3209): splice key from `row.keys` (3255), collapse emptied row (3257-60) + emptied col `ws.columns.splice(colIndex,1)` (3276-81). Both `savePrefs()`+`requestRelayout()`+`bringToFront` (3774-75 / 3932-33). Menu gating: `buildWindowMenu if(win.tiled)` (14151-53); own-column `enabled:!!loc && !alone` (14178, alone=single key/single row 14159-60); new-column `enabled:!!loc` (14184). **MODEL NOTE: live layout = `col.rows[]` each `row.keys[]`(+heights); stacked col = >1 row; row w/ >1 key = side-by-side split (relevant for F030 tabs/F032 splits/F033 stacks).** No gap, no code change.

**Next to pick:** F030 — Tabs (index.html): Alt-drag to stack windows as tabs in one tile; tab strip switches; "Tab into left/right column" / "Tab this window". FRONTEND inspect-only.

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 = index.html):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing change) or run pytest for pure-frontend; real gap → report as finding (don't fix). Only commit every_feature.md + status.md.

**Reminder:** already-built — VERIFY, don't rebuild. Docs/spec vs code disagree → code usually wins (F018/F022), correct the feature line. F045 closed-notes dep F050's + menu; F058 help-MCP-status dep F065 — skip-and-return if before deps. Backend (.py) CAN be patched + pytest-verified. Pre-existing index.html change — leave alone.
