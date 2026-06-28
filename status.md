# Loop status — handoff to next iteration

**Just handled:** F004 — Output ring buffer → **done**. `ByteRing` in `webterm/agent/ringbuf.py`: default cap 256*1024=262144 bytes, deque of chunks, FIFO chunk-granular eviction with an `evicted` flag, `get()` joins retained bytes; consumed by `agent.py` (`ring.get()`/`ring.evicted`) to build tier-1 snapshots via `snapshot/raw.py`. Intentional exception: a single append larger than cap is kept intact (avoid blank snapshot) — test-codified (`test_never_evicts_newest_chunk`), so NOT rewritten. No code change. Verification: py_compile PASS; `pytest -k "ring or buffer or ringbuf"` → 9 passed/0 failed; behavioral asserts a(default cap)/b(<cap retained in order)/c(>cap→tail==last cap bytes, evicted)/d(boundary-crossing chunks) all PASS.

**Next to pick:** F005 — Reconnecting WS producer client (webterm/agent/client.py, agent.py): exponential backoff 0.5s→10s, re-hello with current title/dims, survives broker restarts. First unchecked, no unmet deps.

**In-progress / failed-attempt markers:** none.

**Reminder for implementers:** browserland is an already-built product — VERIFY documented behavior (and patch real gaps), don't rebuild. F045 closed-notes depends on F050's + menu; F058 help-MCP-status depends on F065 — skip-and-return if reached before their deps.
