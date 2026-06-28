# Loop status — handoff to next iteration

**Just handled:** F001 — Wire protocol frames → **done**. Verified `webterm/protocol.py` is the single source of truth for all six control frames (hello/title/resized/input/resize/snapshot_please) + raw binary ANSI/snapshot payloads; all producers/consumers import it, no inline frame-building drift. No code change needed. Verification: `python -m pytest -q -k protocol` → PASS (23 passed).

**Next to pick:** F002 — Linux PTY backend (webterm/agent/backends/linux_pty.py, base.py). First unchecked, no unmet deps. NOTE: this is a Windows host — a Linux-PTY (pty.openpty/TIOCSCTTY) feature may not be runtime-verifiable here; the sub-agent should verify via import/static checks and existing tests, and report `blocked` (host-incompatible) rather than fail if it genuinely cannot run.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
