# Deprecated mods

Mods that were **shipped and then retired**. They are kept here, verbatim and
with their git history, so a retirement is a decision that can be revisited
rather than a deletion that has to be reconstructed from the log.

Nothing in this tree is loaded. `webterm/broker/ui.py`'s `_MODS` is an explicit
allowlist, not a glob (`ui._MODS`), and a retired mod's line is removed from
it — *that* is what un-ships it. The move here is for humans: it keeps
`mods/` equal to "what actually ships", which is also what the drift guard in
`tests/test_ui_assets.py::test_mod_scripts_exist_on_disk_and_match_mods_dir`
asserts (it rglobs `mods/**/*.js` and requires an exact match with `_MODS`).
That is why this is a **sibling** directory and not `mods/_deprecated/`.

## Re-enabling one

The obvious guess is wrong, so it is worth saying plainly: **you cannot
re-enable a retired mod by dropping it into `mods_dir`.** Runtime-installed
mods must use the reserved `x-` prefix (`modinstall._INSTALLED_ID_PREFIX`).
The install validator rejects a reserved id with `400 reserved_id`, and the
scanner skips any non-`x-` directory with a loud log so a hand-dropped folder
can never shadow a shipped mod (#172, `modinstall.py:960`). A copied-back
`agent-docs/` under `mods_dir` is silently ignored.

Re-enabling is a **source-tree** change, and "copy it back" alone is not
enough — the drift guards that keep `mods/` honest will fail until every step
below is done. On a clean checkout:

1. **Copy the folder back.**
   `git mv webterm/broker/mods-deprecated/<id> webterm/broker/mods/<id>`
   Leaving it in both places fails
   `test_mod_scripts_exist_on_disk_and_match_mods_dir`.
2. **Re-add its entry to `_MODS`** in `webterm/broker/ui.py`, in dependency
   order — a mod must be listed **after** every mod it `requires`
   (`test_requires_declared_before_dependency_...` enforces this, and the
   loader's one-pass enable cascade relies on it).
3. **Restore its expectations in `tests/test_ui_assets.py`:**
   - re-add its `_EXPECTED_TIERS` row (the tier list is a hardcoded drift
     sentinel, so a missing mod fails);
   - re-add any `_MOD_CROSS_FRAGMENT_CALL_INS` edges it owns or reaches — that
     set fails on a **stale** edge as loudly as on a new one, so the edges have
     to come back at the same time the code does;
   - rewrite the mod's `..._retired_to_deprecated_tree` test back into the
     packaged-and-shipped assertions it replaced (`git log` has the original).
4. **Regenerate the Help corpus** if the mod ships a `help.md`:
   `python webterm/broker/help_corpus.py`. Its section returns, so bump the mod
   count in `tests/test_help_corpus.py::test_full_corpus_includes_mod_sections`
   too — the corpus is committed and drift-tested.
5. **Restart the broker.** `INDEX_HTML` is assembled and cached at import, so an
   edit needs a restart, not just a reload — and a *browser* that already has
   the old page keeps the old bundle until it reloads. Mods take effect on the
   next page load, always (`wiki/Writing-a-Mod.md` §10.3).
6. **Expect the mod to come back at its DEFAULT, not at each browser's old
   choice.** `webterm:mods:disabled` stores ids toggled *away* from their
   default, and the loader prunes ids it no longer recognises, so anyone who had
   deliberately turned the mod off before the retirement has had that choice
   discarded and will get it back **on**. That is the loader's documented
   trade-off for removed mods, not a bug introduced here — but if the mod is
   hazardous, say so in the release notes rather than assuming the old off
   survived.
7. **Update `README.md`, `wiki/Writing-a-Mod.md` and the wiki** — the retirement edited
   all three.

## Not in the wheel or the sdist — deliberately

`pyproject.toml` finds packages with `include = ["webterm*"]` and globs
package-data one level under `mods/` (`mods/*/*.js`, `*.css`, `*.json`,
`*.md`). This tree is neither a package nor matched by those globs, and there is
no `MANIFEST.in`, so it ships in **neither** artifact — verified by building
both and listing their contents, not just by reading the config.

That is the intended answer, not an oversight: re-enabling means editing
`ui._MODS` in the source tree (step 2), which a pip-installed copy cannot do
usefully, so shipping the files without the mechanism to load them would be dead
weight in every install. **Re-enabling requires the repo.**

---

## `agent-docs` — retired in #177

The tabbed AGENTS.md + CLAUDE.md editor (with the ⚙ Sections tab and the
template checklist) opened from each terminal's 📋 title-bar button.

**Why.** The button had no idea which directory you were in. It took whatever
the session's inferred cwd happened to be (`agent-docs.js:97`, `sess.cwd`) and
opened `<cwd>/AGENTS.md` + `<cwd>/CLAUDE.md`. When that inference was wrong it
opened a *different project's* AGENTS.md, silently, with the same UI it shows
when it is right — and because the window saves, a wrong guess did not just
show the wrong file, it wrote to it.

The bug is not in this mod. `sess.cwd` is **inferred from the OS process tree**,
not reported by the shell: `PtyBackend.cwd()`
(`webterm/agent/backends/base.py:97-139`) BFS-walks the session's descendants
via psutil, picks the agent process closest to the shell (`detect._AGENTS`) and
reports *that* process's cwd, falling back to the shell's when no agent is
found. `agent.py:575-589` polls it on a timer and pushes a `cwd` frame on
change (#156). Every step has a way to be wrong — a `cd` between ticks, the
agent-vs-shell ambiguity (#47) cutting both ways, an unrecognised tool falling
back to the known-wrong parent, multiple or nested agents, and a denied or
absent psutil yielding `None` so the button keeps opening the **last-known**
directory with no sign that it is stale.

Retiring the mod does not fix the inference. It retires the one consumer where
a wrong guess *wrote to disk*. The same cwd still feeds the git-status widget
(`git_status.collect(cwd)`), the file manager's start dir
(`68_js_app_windows_files.js:155`) and the task manager, where being wrong is
cosmetic or self-evident.

Nothing is lost that the desktop cannot already do: AGENTS.md and CLAUDE.md are
ordinary files, and the text editor and file manager still open them. What went
away is the shortcut that guessed which copy you meant.

**Bring it back when the cwd becomes authoritative** — OSC 7 / shell
integration *reporting* the directory instead of psutil inferring it. That is
the change that would have to land first; until then, re-enabling this mod
re-enables the wrong-folder write.

**What stayed behind in `mods/editor/`.** Only the two entry points moved out —
`openAgentDocsWindow` and `openAgentsMdEditor` — plus the 📋 button. The tabbed
`docs` / Sections / template machinery is interleaved in `editor.js` and stays
there, so an **already-stored** Agent-docs window still restores and works
(tabs, Sections, save, Choose folder). Only the ability to open a *new* one from
a terminal is gone.

A stored *legacy* single-doc record (`agentsMdCwd`, no `docs` — from a build
before the tabbed window) used to be upgraded on reopen by calling
`openAgentDocsWindow`. That call is now guarded with
`typeof openAgentDocsWindow === 'function'`, so with the mod unshipped the
record falls through and rebuilds as exactly the pre-#120 single-doc AGENTS.md
editor it was serialized from — its own cached content, its `filePath`, and the
AGENTS save hook intact. **Un-guard nothing when copying this mod back**: the
`typeof` test passes again on its own.

Three residuals of that fall-through, stated rather than hidden:

- **It shows cached text, not a fresh read.** The upgrade path re-read
  `AGENTS.md` from disk; the fall-through restores the buffer the record was
  serialized with, so a file changed on disk since then will look stale and a
  Save will overwrite it. This is how *every* restored text-editor window in
  browserland already behaves — restoring the buffer is what preserves unsaved
  work — and it only reaches records written by a pre-#120 build that have not
  been reopened since. It is not a fresh hazard, but it is a change from what
  the previous release did for this one record shape.
- **Do not "fix" it by re-entering `openAppWindow` with the same record.** The
  branch tests `agentsMdCwd && !docs`, so a re-entry that still carries
  `agentsMdCwd` and no `docs` matches again and loops forever.
  `openAgentDocsWindow` avoided this by passing a `docs` array — which is the
  tabbed window, i.e. the thing being retired.
- **Stored Agent-docs windows stay writable.** That is deliberate. The hazard
  #177 retires is the 📋 button *guessing* a folder; a stored window names a
  folder the user already had open, shows it in its title, and re-guesses
  nothing. Making those windows read-only would destroy working state to solve a
  problem they do not have.

**The tabbed machinery in `editor.js` is now producer-less, on purpose.**
Nothing shipped creates a `docs` record any more — only restore and re-serialize
do. Keeping it is what makes stored windows keep working and what makes
copying this mod back a two-file change instead of a rewrite. Deleting it would
be a separate decision needing a persisted-record migration, and it is not part
of this retirement.

**Stale on/off choices are already a quiet no-op**, by existing machinery, so
there is nothing to migrate:

- `webterm:mods:disabled` holds ids toggled *away* from their declared default.
  An id that is neither registered nor in the `/info` catalog is pruned at boot
  (`86_js_mod_loader.js`), touching no neighbouring entry — and the prune is
  skipped entirely when the boot `/info` did not answer, so a 401 never
  discards choices it could not see.
- A #157 broker pin naming `agent-docs` is dropped by `_resolvePins`, which
  keeps only ids this build actually registered.
- #158 mod-sync is safe in **both** skew directions, which is worth checking
  explicitly because only one of them is obvious. Adopting *from* an older peer
  that still ships agent-docs: adopt walks **our own** registered mods, never
  the peer's catalog, so the extra id is never considered and never errors.
  An older peer adopting *from* this build: it walks *its* registered mods,
  finds agent-docs missing from our catalog, and records it as
  `missing / "not installed there"` — reported in the preview, not applied. An
  omitted row would otherwise read as agreement and silently restore that
  peer's default, which is exactly the failure the `missing` row exists to
  prevent.

One thing no server-side change can undo: a browser that already has the page
keeps the old bundle, 📋 button and all, until it reloads. Mods take effect on
the next page load, always (`wiki/Writing-a-Mod.md` §10.3) — un-shipping is not a
runtime revocation.

**Tests.** Two, in `tests/test_ui_assets.py`.
`test_agent_docs_mod_retired_to_deprecated_tree` asserts the retired copy is
intact here and absent from `_MODS`, the mod catalog and the served page — and
that the editor's call site is guarded.
`test_retired_agent_docs_would_still_load_if_copied_back` is the anti-rot half:
the retired script still passes the portable-mod top-level lint, and every
core/editor name it calls as a hoisted free identifier (`editorFile`,
`openAppWindow`, `joinNative`, `tabWindowIntoTile`, …) is still declared in the
served page. That is how this copy actually rots — the host renames something
out from under it — and `editorFile` is the live risk, since #177 took away its
only external caller.

Re-enabling means pointing both back at `mods/` and restoring the two
`_MOD_CROSS_FRAGMENT_CALL_INS` edges (`editorFile` ← agent-docs,
`openAgentDocsWindow` ← editor) that #177 removed.

**Republishing it as an installable `x-agent-docs` package is a non-goal.** It
and `editor` call into each other's top-level names, which only works because
shipped mods share one concatenated script — see `wiki/Writing-a-Mod.md` §10.2. That is
a decoupling job, not a move.
