# Feature implementation loop — spec

ROLE
Implement every documented feature, one at a time, verifying each before moving on, while keeping the main context window minimal. Sub-agents do the heavy reading and coding; main only orchestrates and tracks progress.

FILES
- every_feature.md — durable record: every feature, what's done, what's blocked. Source of truth for progress.
- status.md — one short handoff note to the next loop. Overwrite every loop; never append. No history (every_feature.md already has it).

SETUP — run ONLY if every_feature.md does not yet exist
- Read the docs, README, specs, and Wiki.
- Write every_feature.md, one checklist line per feature, ordered so each feature's dependencies come before it:
  - [ ] [F001] Name — one-line description
- Size each feature so it can be implemented AND verified in a single sub-agent run. If something is bigger, list its parts as separate items instead.
- If every_feature.md already exists, do not run setup and do not reorder it — resume from where it left off. Never regenerate a list that has checked items.

EACH LOOP
1. Read every_feature.md and status.md. Pick the first item that is unchecked and not blocked, in dependency order. If status.md flags a feature as in-progress, pick that one up first.
2. Spawn a sub-agent with a tight brief: the feature ID + description, the relevant doc section, pointers to the files and conventions to follow, and the exact command(s) that prove it works (build / typecheck / test). Tell it to:
   - implement only that feature,
   - run the verification command(s),
   - reply with ONLY: outcome (done | too-big | blocked), a one-line summary, the verification command + its pass/fail, and — if blocked — the reason. No code, no file dumps, no logs.
3. Act on the reply:
   - done AND verification passed → check the item off ([x]) and commit the working tree ("F00X: <feature>").
   - too-big → split it into smaller unchecked items in every_feature.md; pick up next loop.
   - verification failed → if status.md already shows a failed attempt for this same feature, mark it blocked; otherwise leave it unchecked and record the attempt in status.md to retry once.
   - blocked → move the line to a "## Blocked" section with the reason. Never re-pick a blocked item.
4. Overwrite status.md with only: the feature just handled + its outcome, the next feature to pick, and any in-progress / failed-attempt marker the next loop needs. Nothing else.

FINISH — when no unchecked, unblocked items remain
- Run the full build + test suite yourself, once. Uncheck any feature that now fails and loop again; use the per-feature commits to locate what broke it.
- When everything passes, overwrite status.md with a final summary: number done, plus any blocked items and their reasons. Then stop.
