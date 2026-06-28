# Loop status — handoff to next iteration

**Just handled:** F028 — Move & focus columns (keyboard) → **done** (FRONTEND inspection-verified, index.html not edited). `focusColumnBy(dir)` (idx:13921, tiling-only guard 13922): clamp + `ws.focusedCol=j` (13929) + `savePrefs()` (13930) + `scrollColumnIntoView(j,true)` (13931) + front first live win. `moveColumn(colIndex,dir)` (4073): bounds 4076-77, `ws.columns.splice(colIndex,1)`+`splice(j,0,c)` (4078-79), `ws.focusedCol=j` (4080), `savePrefs()` (4081), `requestRelayout()` (4082), `scrollColumnIntoView(j,true)` (4083). KEY_ACTIONS: focus-col-left/right→focusColumnBy(∓1) (13946-49), move-col-left/right→moveColumn (13950-53). DEFAULT_KEYBINDINGS (2564): focus-col-left=Ctrl+Alt+ArrowLeft (2565), focus-col-right=Ctrl+Alt+ArrowRight (2566), move-col-left=Ctrl+Alt+Shift+ArrowLeft (2567), move-col-right=Ctrl+Alt+Shift+ArrowRight (2568). scrollColumnIntoView (5365). No gap, no code change.

**Next to pick:** F029 — Eject column (index.html): "Move to own column" (un-share a stacked column) and "Move to new column" (spawn column to the right). FRONTEND inspect-only.

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 = index.html):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing change) or run pytest for pure-frontend; real gap → report as finding (don't fix). Only commit every_feature.md + status.md.

**Reminder:** already-built — VERIFY, don't rebuild. Docs/spec vs code disagree → code usually wins (F018/F022), correct the feature line. F045 closed-notes dep F050's + menu; F058 help-MCP-status dep F065 — skip-and-return if before deps. Backend (.py) CAN be patched + pytest-verified. Pre-existing index.html change — leave alone.
