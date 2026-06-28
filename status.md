# Loop status — handoff to next iteration

**Just handled:** F008 — OSC title sniffer → **done**. `webterm/agent/titles.py` `OscTitleSniffer` is a byte-at-a-time stateful parser (partial `_param`/`_payload` buffers across feeds), recognizes OSC 0/2 with BOTH BEL (`\x07`) and ST (`\x1b\\`) terminators, ignores non-title OSC (param != 0/2), caps payload at 4 KiB (`_MAX_PAYLOAD`) abandoning to GROUND on overflow. Wired in `agent.py:270-275` → `protocol.title_frame` → `SessionState.title`. No code change. Verification: py_compile PASS; `pytest -k "title or osc or sniff"` → 69 passed/0 failed; behavioral a–f all PASS (a whole; b split across feeds; c introducer split mid-ESC; d ST terminator; e OSC 8 ignored; f 100k never-terminated → bounded at 4096, no emit).

**Next to pick:** F009 — Alt-screen + DECCKM tracking (webterm/agent/altscreen.py): track alternate-screen and application-cursor-key mode live off the PTY stream for read/send_keys. First unchecked, no unmet deps.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
