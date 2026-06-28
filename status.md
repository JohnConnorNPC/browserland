# Loop status — handoff to next iteration

**Just handled:** F023 — Floating windows → **done** (FRONTEND, inspection-verified, index.html not edited). All 3 behaviors present: drag-move `wireDrag` (idx:12354, onDown→bringToFront + left/top from cursor delta, onUp persists geom; bound to title bar 12585 + call sites 7850/10324/11143/11731/15127); edge/corner resize `wireResize` (idx:12589, 8 handles `.rh rh-<dir>` n/s/e/w/nw/ne/sw/se created 7506-7511, adjusts w/h/left/top by dir, onUp→sendResize); z-order `bringToFront`/`floatZIndex` (idx:7315/6482, monotonic `nextZ` 6476, set `style.zIndex` on focus; bound to window mousedown 7574-7575 etc). All floating-only: move short-circuits `if(win.tiled) startTiledDrag` (12361), resize `if(win.tiled) return` (12592) + CSS `.tiled .rh{display:none}` (1108), bringToFront tiled→column-focus branch returns before zIndex. No unit test (pure frontend, expected). No code change.

**Next to pick:** F024 — Tiling mode + scrolling strip (index.html): niri-style horizontal strip of non-overlapping columns; strip scrolls (scrollbar, drag-to-edge). FRONTEND inspect-only.

**In-progress / failed-attempt markers:** none.

**FRONTEND VERIFICATION MODE (F023-F062 are index.html features):** verify by INSPECTION ONLY — read-only grep/read of `webterm/broker/index.html`; confirm the documented behavior has real implementing JS with file:line evidence; do NOT edit index.html (pre-existing uncommitted change) or run pytest for pure-frontend behavior; if a real gap, report as finding (don't fix). Only commit every_feature.md + status.md.

**Reminder:** browserland is already-built — VERIFY documented behavior, don't rebuild. Where docs/spec disagree with code, code usually wins (F018/F022) — verify + correct the feature line. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before deps. Backend features (.py) CAN be patched + pytest-verified; only commit every_feature.md + status.md (+ real .py fix). Pre-existing index.html change — leave alone.
