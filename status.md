# Loop status — handoff to next iteration

**Just handled:** F005 — Reconnecting WS producer client → **done**. `webterm/agent/client.py` reconnect loop `while not self._stopping` catches connect-failure + session-end so it survives broker restarts; backoff `_BACKOFF_INITIAL=0.5`/`_BACKOFF_CAP=10.0` via `min(b*2, cap)` → series [0.5,1,2,4,8,10,10], reset to 0.5 on successful connect; `_serve` re-sends hello every connect via `protocol.hello_frame(window_id,pid,title,cols,rows,…)` reading live `SessionState` (title from `_on_pty_data`, dims from `_on_resize`) — not captured-once; uses F001 builder. No code change. Verification: py_compile PASS; `pytest -k "client or reconnect or backoff or producer or agent"` → 21 passed/1 skipped/0 failed; backoff series helper-driven from real constants PASS.

**Next to pick:** F006 — Snapshot rendering (raw + pyte) (webterm/agent/snapshot/raw.py, pyte_snap.py): tier-1 `ESC[0m ESC[2J ESC[H` + ring replay; optional tier-2 pyte settled-grid render via --snapshot-mode. First unchecked, no unmet deps (builds on F004 ring, done).

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
