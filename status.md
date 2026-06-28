# Loop status — handoff to next iteration

**Just handled:** F002 — Linux PTY backend → **done**. `webterm/agent/backends/linux_pty.py` matches the contract (pty.openpty + Popen with start_new_session + loop.add_reader + TIOCSCTTY ioctl via preexec_fn; fd-safe error path) and implements base.py's `PtyBackend`; POSIX-only import is lazily guarded in `__init__.py` (`os.name != "nt"`) so Windows never imports termios/pty. No code change. Verification (Windows host): `python -m py_compile …backends/{linux_pty,base,__init__}.py` PASS; `python -m pytest -q -k "pty or backend"` → 23 passed, 10 skipped, 0 failed (Linux PTY tests skip on Windows as designed).

**Next to pick:** F003 — Windows PTY backend + auto-select (webterm/agent/backends/win_conpty.py, __init__.py). First unchecked, no unmet deps. This IS the native host (Windows) — winpty/ConPTY is runtime-verifiable here.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
