# Loop status — handoff to next iteration

**Just handled:** F006 — Snapshot rendering (raw + pyte) → **done**. Tier-1 `webterm/agent/snapshot/raw.py` `render` emits exact prefix `\x1b[0m\x1b[2J\x1b[H` (reset SGR / clear / home) then replays ring bytes (`agent._on_snapshot_request` feeds `self.ring.get()` from F004 ByteRing). Tier-2 `pyte_snap.py` (pyte 0.8.2 installed; `available()`→True) renders settled grid; `--snapshot-mode` wired via cli.py (choices raw/pyte, default raw)→config→agent; `__main__.py` fast-fails exit 2 if `pyte` requested without pyte; pyte path wrapped in `except Exception`→falls back to raw, no crash. No code change. Verification: py_compile PASS; `pytest -k "snapshot or raw or pyte"` → 191 passed/9 skipped/0 failed; tier-1 prefix+replay PASS; tier-2 non-empty grid PASS; fallback PASS.

**Next to pick:** F007 — Dependency-free bounded text-grid renderer (webterm/agent/snapshot/textgrid.py): in-house emulator producing a bounded rows×cols grid for MCP read when pyte is absent. First unchecked, no unmet deps.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
