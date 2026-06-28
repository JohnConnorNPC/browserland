# Loop status — handoff to next iteration

**Just handled:** F012 — Per-window git status → **done**. `webterm/agent/git_status.py` `collect()` shells `git -C <cwd> status --porcelain=v2 --branch` hardened (shell=False, stdin DEVNULL, prompt-disabling env, daemon drain threads w/ 1MiB/8KiB caps, hard `_TIMEOUT`=5.0s → kill+`{ok:False,error:"timeout"}`). Returns dict: success `{ok:True, branch, detached, ahead, behind, staged, unstaged, untracked, conflicts, dirty, dirty_count}`; failure `{ok:False, error}` (non-repo→`not_a_repo`, no cwd→`no_cwd`, missing git→`git_not_found`, else str[:200]); never raises. On-demand RPC (NOT detect loop): broker `git_status_please`→client `on_git_request`→agent run_in_executor→`protocol.git_status_frame`. No code change. Verification: py_compile PASS; `pytest -k "git or status or detect"` → 18 passed/0 failed; live on this repo `{branch:'main',dirty:True,ahead:12,untracked:1}` consistent; non-repo→not_a_repo; monkeypatched FileNotFound→git_not_found, TimeoutExpired→timeout, no raise.

**Next to pick:** F013 — Task-manager backend contract (webterm/agent/agent.py): enumerate_procs child tree, End a process, destroy a session (identity-checked psutil path + Linux SID kill fallback); the enumerate/destroy contract F048's UI drives. First unchecked, no unmet deps.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
