# Loop status — handoff to next iteration

**Just handled:** F010 — Foreground coding-agent detection → **done**. `webterm/agent/detect.py` `_AGENTS=("claude","grok","codex","opencode")` (== broker whitelist registry.py:34); `classify_proc` strict layered match (exe basename → argv[0] basename → first non-option script arg for node/python/pwsh wrappers → vendor-install-dir fallback .grok/.claude/.codex/.opencode), false-positive guards confirmed (`rg codex`/`cat claude.md`/`node server.js codex`→None). Walk rule: Windows scans shell+all descendants→first match; Linux gates on true foreground pgrp via tcgetpgrp (fallback full walk). Graceful: psutil ImportError/bad-pid/raising name()/cmdline()/exe() all→None, never raises. Wired state.agent→registry→/sessions + list_terminals. No code change. Verification: py_compile PASS; `pytest -k "detect or agent or foreground or badge"` → 23 passed/2 skipped/0 failed; name-matching + graceful-degradation behavioral PASS.

**Next to pick:** F011 — Live cwd tracking (psutil, best-effort) (webterm/agent/agent.py, config.py): report the shell's current working directory; degrades to None without psutil. First unchecked, no unmet deps.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
