# Loop status — handoff to next iteration

**Just handled:** F003 — Windows PTY backend + auto-select → **done**. `win_conpty.py`'s `WinConPtyBackend(PtyBackend)` implements all six base methods, uses `winpty.PTY`, daemon reader thread pumps output via `loop.call_soon_threadsafe` (on_exit after last on_data), resize/kill present; `__init__.py` routes `os.name=="nt"`→WinConPty, `_auto_backend()` picks ConPTY when GetConsoleWindow()!=0 else WinPTY, conpty/winpty force it. No code change. Verification (Windows, pywinpty 3.0.3): py_compile PASS; import PASS; `pytest -k "conpty or winpty or backend or windows"` → 19 passed/6 skipped/0 failed; real smoke spawn (`cmd /c echo BROWSERLAND_OK` via auto→WinPTY) delivered output, exit 0, kill joined thread, no stray proc.

**Next to pick:** F004 — Output ring buffer (webterm/agent/ringbuf.py): bounded recent-output ring (default 262144 bytes) with eviction, the source for snapshots. First unchecked, no unmet deps. Pure-Python, fully runtime-verifiable here.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
