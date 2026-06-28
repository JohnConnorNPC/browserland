# Loop status — handoff to next iteration

**Just handled:** F031 — Untab → **done (split)**. 3 of 4 documented mechanisms PRESENT & verified: "Untab tile (split to rows)" `untabTile(col,rowIndex)` (idx:3287-3297, rebuild tabbed row→one single-win row per key, normalizeRowHeights+savePrefs+requestRelayout; menu 14208-10); "Untab cell (side by side)" `nestedUntabCell(row,cell)` (3341-3357, splice group keys→sibling leaf cells, redistribute widths, savePrefs+requestRelayout; menu 14218-19 gated to groups 14215); tab-strip ⊟ button `.strip-tab-untab` (layoutRowTabs 4794-4805, `textContent='⊟'`, click→nestedUntabCell|untabTile, CSS 1268). **GAP→F082 (Blocked):** "drag a tab out" gesture NOT implemented though in-app help (14647-48) promises it (`.strip-tab` mousedown only stopPropagation/click switches activeTab; strip-bg drag moves whole group; startTiledDrag no single-tab detach). Split off to ## Blocked F082; needs index.html edit (implement OR remove help claim) — deferred (pre-existing index.html change blocks clean frontend commits) + human decision. No code change.

**⚠ SURFACED TO USER:** first real product gap — F082 doc/behavior mismatch (help promises tab drag-out that isn't built).

**Next to pick:** F032 — Splits (index.html): drop on a window's left/right interior for side-by-side panes; drag the gutter to resize; un-split/merge. FRONTEND inspect-only. (NOTE: skip the now-Blocked F082; F032 is next unchecked unblocked.)

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 = index.html):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing change) or run pytest for pure-frontend; real gap → report finding, split working part [x] + gap to ## Blocked (don't fix). Only commit every_feature.md + status.md. MODEL: `col.rows[]` each `row.keys[]`(+heights), `row.mode` single/tabbed/split; nested `cell.keys[]`+activeTab.

**Reminder:** already-built — VERIFY, don't rebuild. Docs/spec vs code disagree → code usually wins (F018/F022), correct the feature line; but a code-vs-DOC+HELP gap = real finding (F082). F045 closed-notes dep F050's + menu; F058 help-MCP-status dep F065 — skip-and-return if before deps. Backend (.py) CAN be patched + pytest-verified. Pre-existing index.html change — leave alone.
