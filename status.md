# Loop status — handoff to next iteration

**Just handled:** F027 — Column gutter drag-resize → **done** (FRONTEND inspection-verified, index.html not edited). Gutter `.strip-col-gutter` (`cursor:col-resize` idx:1183-1198, `body.col-resizing` lock 1198-1199); `buildColGutter()` creates div, stamps `data-leftColId`/`data-rightColId`, binds `mousedown→onColGutterDown` (5099-5106); stamped between every adjacent col pair at layout (5332-5337). `onColGutterDown` (5107-5146): `dfrac=(clientX-startX)/stripW` (5129), clamp minF/pairSum, write `leftCol.widthFrac=nl`/`rightCol.widthFrac=nr` (5132-5133, pair-sum constant→rest stationary), live widths 5134-5135; onUp `savePrefs()`+`requestRelayout()` (5141-5142). Layout precedence `widthFrac>0 ? widthFrac : presetFraction(widthPreset)` (5302-5306, colCurrentFrac 5095-5098); F026 `setColumnPreset` `delete loc.col.widthFrac` (13849) → preset vs custom mutually exclusive; sanitizer validates/deletes bad widthFrac (3467-3469). No gap, no code change.

**Next to pick:** F028 — Move & focus columns (keyboard) (index.html): focus/move column left/right, bringing off-screen columns into view. FRONTEND inspect-only.

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 = index.html):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing change) or run pytest for pure-frontend; real gap → report as finding (don't fix). Only commit every_feature.md + status.md.

**Reminder:** already-built — VERIFY, don't rebuild. Docs/spec vs code disagree → code usually wins (F018/F022), correct the feature line. F045 closed-notes dep F050's + menu; F058 help-MCP-status dep F065 — skip-and-return if before deps. Backend (.py) CAN be patched + pytest-verified. Pre-existing index.html change — leave alone.
