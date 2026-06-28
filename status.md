# Loop status — handoff to next iteration

**Just handled:** F011 — Live cwd tracking → **done**. `agent.py:_detect_loop` (L318-332) polls `backend.cwd()` in a thread every `_DETECT_INTERVAL`=1.5s, updates `SessionState.cwd` before enqueuing `protocol.cwd_frame` (so reconnect hellos stay accurate, `cd` reflected live). `backends/base.py:cwd()` reads via psutil and reports the AGENT's cwd (BFS-closest agent via `_find_session_agent`+`classify_proc`), falling back to the shell's cwd only when no agent — matches #47 "report the foreground agent's cwd, not the shell's". Degrades to None on every failure (psutil ImportError/bad-pid/unreadable). Flows hello/cwd frame→registry `entry.cwd`→`summary()`→/sessions→list_terminals. No config toggle documented. No code change. Verification: py_compile PASS; `pytest -k "cwd or agent or session or psutil"` → 28 passed/3 skipped/0 failed; behavioral (a live read == os.getcwd via samefile; b bad-pid/None/psutil-absent→None no raise; c agent-vs-shell selection by tests) PASS.

**Next to pick:** F012 — Per-window git status (webterm/agent/git_status.py): surface git state of the window's cwd. First unchecked, no unmet deps (builds on F011 cwd, done).

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
