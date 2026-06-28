# Loop status — handoff to next iteration

**Just handled:** F009 — Alt-screen + DECCKM tracking → **done**. `webterm/agent/altscreen.py` `DecModeSniffer` exposes `.alt_screen` (DECSET/RST 47/1047/1049) and `.app_cursor` (DECCKM mode 1) booleans updated by `feed(bytes)`; regex parses `;`-separated params numerically (combined params + leading-zeros safe; no `?470h`/`?10h` false positives); split introducers reassembled via 64-byte `_tail` carry. `agent.py` feeds every PTY chunk (L262), uses `.alt_screen` for snapshot screen-vs-scrollback, emits `mode_frame` + stores `state.app_cursor` on change (surfaced in read_screen + list_terminals/F080). No code change. Verification: py_compile PASS; `pytest -k "alt or altscreen or deckm or decckm or cursor or mode"` → 38 passed/0 failed; behavioral a–f all PASS (incl. legacy 47 & 1047, split reassembly, combined `?1049;1h`→both).

**Next to pick:** F010 — Foreground coding-agent detection (webterm/agent/detect.py): detect claude/codex/grok/opencode as the foreground command for the agent badge. First unchecked, no unmet deps.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
