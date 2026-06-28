# Loop status — handoff to next iteration

**Just handled:** SETUP — created `every_feature.md` (81 features, F001–F081, dependency-ordered), codex-reviewed it, then applied a conservative refinement (added F077–F081 for confirmed-missing documented features; edited F013/F019/F044/F045/F048/F058/F062/F068 for overlaps, the F019/F068 cwd contract, and two dependency notes). All 81 boxes unchecked.

**Next to pick:** F001 — Wire protocol frames (webterm/protocol.py). First unchecked, no dependencies.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — most features already exist in code. Each sub-agent should VERIFY the documented behavior (and patch any gap), not rebuild from scratch. F045 closed-notes depends on F050's + menu and F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
