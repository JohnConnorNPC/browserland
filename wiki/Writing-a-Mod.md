<!-- help:tier dev -->

A **mod** is a folder of JavaScript (plus optional CSS and an in-app help page)
that extends the Browserland desktop: taskbar chips, app-window kinds, Control
Panel settings, per-terminal title-bar widgets, menu items, key actions. A set
of them ships in the repo, under `webterm/broker/mods/` (the README lists them),
and a broker can also be handed one at runtime through `POST /mods/install`.

This document is the authoring contract. It is derived from
`webterm/broker/86_js_mod_loader.js`, `86a_js_mod_settings_text.js`,
`86b_js_mod_packages.js`, `ui.py`, `modinstall.py` and `help_corpus.py` — when
this file and those disagree, **they are right and this is stale**; please fix
it.

---

## 1. Read this first: `ctx` is not a sandbox

A mod is **same-origin code running inside the desktop page, with the page's
full authority**. It is not isolated, not sandboxed, and not confined by
anything in the `ctx` object it is handed.

Concretely, a mod can:

- read the browser's auth token, and every configured remote host's token;
- `fetch` `/file/write`, `/file/delete`, `/session/kill`, `/state`, `/mcp/*`
  and every other route directly, exactly as core does;
- reach into `prefs`, `windows`, the layout blob and every other core global,
  because they are ordinary bindings in the same script scope;
- rewrite `window.__mods` itself, including the loader functions that are
  supposed to police it.

So **`ctx` grants no privilege a mod does not already have**. It is a set of
*reviewed choke points*, not a permission boundary. Its purpose is that
"don't merge a mod that reads `~/.ssh`" stays a decision a reviewer can make
from one call site instead of auditing every line. The loader says this itself,
in the fragment header and again on `ctx.file`, `ctx.serverStore`,
`ctx.session` and `ctx.clipboard`.

The consequence for the runtime-install feature is the whole of its trust
model: **the trust event is the operator's token-gated install, not the fetch
that follows.** `POST /mods/install` requires the browser auth token; the
`/mods/<id>/<gen>/<file>` route the page then loads from is public, and has to
be, because a `<script src>` cannot carry an `Authorization` header. Installing
a mod is choosing to run somebody's code with this broker's full authority.
Review it the way you would review a patch to the broker itself.

Two corollaries that people get wrong:

- **`tiers` is declared, not enforced** — see §5.
- Nothing in this document is a security guarantee. The caps, the filename
  grammar and the CSS-egress check in the install validator are hygiene and
  defence-in-depth; a mod's own JS can `fetch()` anything it likes once it runs.

---

## 2. The two kinds of mod

|  | **Shipped** | **Installed** |
|---|---|---|
| Lives in | `webterm/broker/mods/<id>/` (in the repo) | `<mods_dir>/<id>/<gen>/` on the broker (config `mods_dir`, default `<state dir>/webterm_mods`) |
| Reaches the page by | being spliced into the one inline `<script>` at import (`ui._MODS`) | a separate classic `<script src="/mods/<id>/<gen>/<file>.js">` injected at boot |
| Id | must **not** start with `x-` | **must** start with `x-` |
| Load list | `ui._MODS`, an explicit list | `/info`'s `mods` catalog, from the broker's in-memory index |
| Manifest names its scripts via | nothing (`entry` is inert; `_MODS` decides) | `scripts`, which is **required** |
| Default enabled | whatever `defaultEnabled` says | always `false`, whatever the manifest says |
| Changing it needs | a broker restart (the page is assembled at import) | a page reload (never a process restart) |
| Its source is | public (it is in `GET /`) | public (`GET /mods/<id>/<gen>/<file>`) |

Everything in §3–§9 applies to both. §10 is the extra contract an installed mod
has to meet.

---

## 3. The smallest mod that works

`mods/clock/clock.js` is the reference mod; this is its shape:

```js
registerMod({
    id: 'clock',
    version: '1.0.0',
    ctxVersion: 1,
    tiers: ['taskbar', 'settings'],
    init: function (ctx) {
        const chip = document.createElement('div');
        chip.id = 'clock-chip';
        ctx.taskbar.addStatusItem(chip);      // removed automatically on teardown

        let timer = setInterval(function () {
            chip.textContent = new Date().toLocaleTimeString();
        }, 1000);
        ctx.onUnload(function () { clearInterval(timer); });
    },
});
```

The rules that shape it:

- **Top level does nothing but call `registerMod({…})`.** Registration is
  bookkeeping; all work happens in `init(ctx)`.
- **`init` is synchronous by contract.** `initMod` calls it and *ignores any
  returned promise*. Kick async work off inside it, but everything another mod
  or core might observe (a registered window kind, a settings control) must
  exist by the time it returns.
- **Everything `init` does must be reversible.** A mod can be disabled live from
  Control Panel → Mods, and its teardown has to leave no timers, listeners or
  DOM behind. `ctx.onUnload(fn)` registers a teardown; they run LIFO. Most `ctx`
  primitives auto-register their own teardown (the taskbar item above removes
  itself), so you only need `onUnload` for what you created yourself.
- **A throw in `init` disables only that mod.** `initMod` catches it, runs the
  partial teardown, releases the slot and logs; core and every other mod keep
  running. That isolation is why the "make it throw" bugs in §11 are so easy to
  ship without noticing.

`registerMod` itself throws — synchronously, as your script executes — on a
malformed declaration (no object, no string `id`, no function `init`) or a
duplicate id. Fields it normalizes: `version` (non-string → `'0'`),
`ctxVersion` (non-number → `null`, meaning "pin nothing"), `tiers` and
`requires` (non-arrays → `[]`, non-string members dropped), `defaultEnabled`
(anything but literal `false` → `true`).

---

## 4. `mod.json`

Every shipped mod has one. It is read by **Python** — `ui._mod_css`,
`ui.mod_catalog`, `help_corpus.build_mod_sections` — and never by the loader.
The `registerMod({…})` call is read by the **loader** and never by Python. Some
fields therefore exist in both places, and a test
(`test_mod_catalog_matches_the_registerMod_declarations`) keeps the copies
honest in both directions.

Every field at once — no real manifest uses all of them:

```jsonc
{
  "id": "notes",                    // the id; matches the directory name by convention
  "version": "1.0.0",
  "ctxVersion": 1,
  "title": "Notes",                 // shown in the catalog / remote policy editor
  "description": "A short sentence an operator will read.",
  "defaultEnabled": false,          // optional; absent == true
  "requires": ["editor"],           // optional; ids of mods that must be ACTIVE
  "styles": ["notes.css"],          // optional; bare filenames in this mod's dir
  "permissions": ["file"],          // closed vocabulary; checked only via the install door — see §10.5
  "help": {                         // optional; only meaningful with a help.md
    "slug": "notes", "label": "Notes", "order": 2100, "icon": "📓"
  },
  "entry": "notes.js",              // INERT — see below
  "author": "JohnConnorNPC"         // INERT — see below
}
```

Field by field:

- **`id`** — used by `ui.mod_catalog()` (falling back to the directory name if
  absent or non-string) and by `help_corpus` as the section's mod tag. It must
  match the `id` in `registerMod` or the catalog and the loader disagree.
- **`version`, `title`, `description`** — catalog text. `title` falls back to
  the id. These are what another broker's Control Panel shows when it edits this
  broker's mod pins, so write `description` for an operator, not for yourself.
- **`ctxVersion`** — informational in `mod.json`. The one that *acts* is the
  number in `registerMod`: a mismatch with the loader's
  `window.__mods.ctxVersion` makes `initMod` refuse the mod outright, and
  omitting it means "pin nothing". The contract is at `1`, and every capability
  added to `ctx` since has been additive — so **feature-detect**
  (`if (ctx.theme)`) rather than declaring a higher number.
- **`defaultEnabled`** — `false` ships the mod off until the operator opts in.
  Absent means on. Mirrored in `registerMod`. For an **installed** mod this
  field is accepted and then ignored: the broker always reports
  `default_enabled: false`.
- **`requires`** — see §6.
- **`styles`** — the only field that changes the served bytes. Each entry must
  be a **bare** `<file>.css` name (`ui._is_bare_css` rejects `/`, `\`, `..`,
  absolute paths, nested dirs and non-`.css`), must exist, must be valid UTF-8
  with **no BOM**, must **end in a newline**, and must be ≤ `ui._MAX_LINES`
  (2500) lines. Any failure is logged and the stylesheet is silently dropped
  from the page — the strict version of the same check lives in
  `tests/test_ui_assets.py` and fails CI.
- **`permissions`** — a closed-vocabulary declaration of which `ctx` capability
  families this package's source actually uses. Every shipped `mod.json`
  carries one, and it is read twice: the broker joins it onto the shipped rows
  of the `/info` catalog, so Control Panel → Mods can show it, and
  `tests/test_ui_assets.py` sweeps `mods/**` to keep every declaration
  truthful and inside the vocabulary. What the shipped copy does *not* get is
  the install-time check: only the **installed**-mod door
  (`modinstall.validate_package`, run by both `POST /mods/install` and the
  `mods_dir` scanner) lints a declaration against the source at the moment it
  is stored, which is why the pane words the two cases differently. The full
  rules — the vocabulary, the refusal, the absent-vs-`[]` distinction — are in
  §10.5.
- **`help`** — `slug` / `label` / `order` / `icon` for the in-app Help section
  (§9). All optional; slug defaults to the mod id, label to `title`, order to a
  computed value after the wiki pages.
- **`entry`** — **inert.** Every shipped manifest declares it, and the test suite
  pins its value for each one, but **no runtime code reads it.** What actually
  loads is `ui._MODS`, an explicit list of `mods/<id>/<file>.js` paths. Editing
  `entry` changes nothing. (The `editor` mod ships *two* scripts, `codemirror.js` and
  `editor.js`, which is exactly the case a single `entry` cannot express.)
  The installed-mod manifest replaces it with a required `scripts` **list**, and
  the install validator **rejects** `entry` as an unknown key.
- **`author`** — **inert.** Every shipped manifest declares it; nothing reads
  it, nothing asserts it, and it is not part of the id namespace. Second-level
  scoping for an installed mod is a convention baked into the *id*
  (`x-<author>-<name>`), not this field — see §10.
- Anything else — ignored by the shipped-mod readers, and **rejected** by the
  install validator (`unknown_manifest_key`), so a typo is loud rather than
  silent.

Note what is *not* in `mod.json`: **`tiers`**. It lives only in the
`registerMod` call, and `ui.mod_catalog()` deliberately does not report it.

---

## 5. `tiers` — declared, not enforced

```js
tiers: ['file', 'window'],
```

`tiers` is a **self-declared** list of the `ctx` capability families a mod uses.
The Mods pane renders them as badges next to the enable checkbox so an operator
can see what a mod touches before switching it on; a mod that declares none
renders as `unspecified`.

Nothing enforces it. The loader filters out non-strings and stores the rest. A
mod that declares `tiers: []` and then calls `ctx.file.delete()` is not stopped,
warned or logged — because, per §1, it could have called `/file/delete` directly
anyway. **A tier declaration is a claim a reviewer checks, not a capability the
runtime grants.**

The vocabulary in use across the shipped set is `settings`, `taskbar`, `window`,
`file`, `session`, `storage`, `clipboard`. It is open — no list is validated —
and it is deliberately imprecise at the edges: `mods/mod-sync/mod-sync.js`
declares `tiers: ['settings']` while administering *other brokers'* mod pins,
because the vocabulary has no token for that and inventing one would imply an
enforcement that does not exist.

---

## 6. `requires`, and how load order is decided

```js
requires: ['editor'],
```

Each listed id must be an **active** mod for this one to `init`. If any is
missing, `initMod` returns `{ok:false, reason:'requires', missing:[…]}` — the
mod is cleanly blocked, with no slot claimed and no partial init, and the Mods
pane shows `needs: editor`. Enabling the dependency later brings the dependent
up through the same cascade (`_bringUp`); disabling a dependency tears its
dependents down first (`_takeDown`).

**Order is resolved at runtime.** `_topoSortRegistered()` (in
`86b_js_mod_packages.js`) runs at boot, after the installed packages have
loaded and before pins are resolved, and reorders `window.__mods.registered`
**in place** so a dependency always precedes its dependents. That in-place
reorder is what lets `_bringUp`, `_takeDown`, `_applyPolicyLive` and
`_resolvePins` keep their single forward/backward passes.

- **Kahn's algorithm** gives the order, with ties broken by the *current*
  registration index — so when nothing installed participates the sort is the
  identity permutation and the shipped order is preserved exactly.
- **Kahn's residual is not the cycle set.** For `A→B, B→A, C→A` the residual is
  `{A,B,C}`, but `C` is merely blocked *by* a cycle. So the residual is split
  with **Tarjan**: an SCC of size > 1 (or a self-loop) is `cycle`, everything
  else in the residual is `blocked-by-cycle`. `initMod` refuses a `cycle` mod
  with its own `{ok:false, reason:'cycle'}` rather than letting it read as an
  ordinary dependency block.
- The graph is built over **shipped ∪ installed** ids. An edge to an id in
  neither is *dropped* from the sort and recorded in
  `window.__mods.missingRequires` — it must not contribute an indegree, or an
  installed mod requiring a shipped mod would come out marked cyclic. An edge to
  an id that the broker knows but this page did not register (its package 404'd,
  or it is off) is not an edge and not "missing" either; the row reads `blocked`.
- The broker does the same analysis over the installed half, in
  `modinstall.catalog()` (`_kahn` + `_cycle_members`), and stamps each row with
  `error: null | "requires_cycle" | "blocked_by_cycle"`. The **client's verdict
  wins** where they differ, because the client sees the whole graph and the
  broker only sorts the installed half.

**For shipped mods the positional rule survives as a style rule.**
`test_requires_declared_before_dependency_in_mods_list` still asserts that every
`requires` names a known mod appearing *strictly earlier* in `ui._MODS`. It used
to be the only thing standing between the project and a cycle; now it is there
because it keeps the shipped half of the graph readable top-to-bottom. It still
enforces all three of: the positional order, that the dependency exists, and
that a shipped `requires` never names an `x-` id (so shipped→installed edges
are unrepresentable, which is what lets `/info` emit every shipped row first).

`requires` is rare in the shipped set, and where it appears it is a mod reusing
the `editor` mod's window kind and its single shared CodeMirror build.

---

## 7. How a shipped mod reaches the page

There is **no `index.html` in this repo.** `ui.py` assembles the served page at
**import time** from the ordered fragments beside it, in five segments:

```
core fragments up to 15_css_dialogs.css
  + every mod stylesheet declared in a mod.json `styles`
  + the rest of core up to 90_js_mod_boot.js
  + every path in _MODS, in order
  + 90_js_mod_boot.js and the tail
```

joined with the **empty string** (every fragment already ends in its own
newline), and the result is `ui.INDEX_HTML`, captured once onto
`app.ctx.index_html`.

Consequences you will hit:

- **A fragment or mod-script edit needs a broker restart.** The page is built at
  import and cached; nothing re-reads the tree per request. Editing
  `mods/clock/clock.js` and reloading the browser shows you the old bytes. This
  is the single most common way to waste an hour. (`curl` the served page and
  grep it if you are unsure what is actually being served.)
- **Every mod script lands in ONE `<script>`, sharing one scope** with core and
  with every other mod. Top-level `const`/`let`/`function` names are global. A
  `const`/`let`/`class` collision is a *compile* error for the whole bundle — a
  blank desktop, not one broken mod — and a duplicate `function`/`var` is worse,
  because it silently overwrites. Prefix everything you declare at top level.
- **A literal `</script>` anywhere in a mod script terminates the element** and
  breaks the page. Split it (`'<' + '/script>'`).
- **`_MODS` is an explicit list, not a glob**, and the drift guard
  (`test_mod_scripts_exist_on_disk_and_match_mods_dir`) is **bidirectional**:
  every path in `_MODS` must exist, and every `*.js` under `mods/` must be
  declared. A new mod file that is not in `_MODS` fails CI; so does a stray
  scratch copy left in a mod directory.
- The CSP is `script-src 'sha256-<hash of the inline script>' 'self';
  frame-ancestors 'none'`. The hash is computed from the very bytes served, so
  adding a mod moves it automatically — but it also means the page must have
  exactly **one** inline `<script>`, and `ui.inline_script_hash` raises when the
  app is built if that stops being true.
- **A mod's CSS is spliced in regardless of whether the mod is enabled.** It is
  present-but-inert: its selectors match nothing until the mod's JS adds the
  markup. Same posture as the spliced-but-not-initialized JS.

Adding a shipped mod is therefore: create `mods/<id>/`, write `mod.json` and
`<id>.js`, append the script path to `ui._MODS` **after any mod it requires**,
and restart the broker.

---

## 8. The `ctx` surface (contract v1)

`makeCtx(modId, rec)` builds a fresh object per mod. Everything below is
additive to v1 — `ctxVersion` stays `1`, so **feature-detect** rather than
bumping it.

### Lifecycle

- **`ctx.id`**, **`ctx.ctxVersion`** — this mod's id, and the loader's contract
  version.
- **`ctx.onUnload(fn)`** — register a teardown. LIFO, each isolated in its own
  `try`. Register it **before** anything that can throw (§11).

### Storage

- **`ctx.storage`** — `get(key)` / `set(key, value)` / `remove(key)` over
  `localStorage`, namespaced `webterm:mod:<id>:<key>`. **Per-browser**, string
  values, never synced. Every call swallows storage failures (private mode,
  quota) and `get` returns `null`.
- **`ctx.serverStore`** — a durable per-mod key/value store on the broker
  (`/mod-store/<modId>`), scoped to this mod's id so one mod can neither read
  nor write another's. Every call resolves to the parsed body plus the HTTP
  `status`, and never rejects: `get(opts)` → `{status, rev, value,
  revisions:[{rev,ts}]}`; `set(value, baseRev, opts)` → `{status, ok, rev, …}`
  (a `409` carries the live value inlined so you can rebase in one round trip);
  `getRevision(n, opts)` → `{status, ok, rev, value}`. Writes ride the `/state`
  single-active-client lease: a non-active browser reads fine but its `set()`
  resolves `409 {error:'not_active'}`. `opts.host` routes to another broker by
  host id, and an unknown id **fails closed** — `{status:0, error:'no_host'}`
  with no request made. Reading another broker is why `status` matters: a `401`
  ("it refused our password") and a `200` with `value:null` ("nothing published
  there") are the same empty body and only the status tells them apart. The
  status is applied *after* the body, so a response body carrying its own
  `status` cannot overwrite the transport's.

  **A mod that stores credentials MUST set `opts.noHistory = true` on every
  write.** The store keeps a revision ring, so without it the value each write
  *replaces* — the previous password, in the clear — stays readable through
  `getRevision(n)` and on the broker's disk. The flag is sticky per record: it
  suppresses new revisions, clears the ring on the write that sets it, and
  survives later writes that omit the option (absent means "leave it alone",
  which is what makes a 409 rebase safe — pass the same `opts` to the retry).
  Turning it back on is deliberate and one-way: `noHistory:false` is refused
  unless `purgeRevisions:true` rides the same call. Only a strict boolean
  reaches the wire, and a broker that predates the flag silently ignores it —
  check `modstore.noHistory` in that broker's `/info` **before** sending a
  credential, because the write that would stop the archiving is itself the
  write that archives.

### Host I/O

- **`ctx.file`** — `read` / `write` / `list` / `delete` / `upload` / `mkdir` /
  `copy` / `move` / `zip` / `unzip` / `stat` / `setattr` / `hash`, plus the
  chunked family (`readChunk`, `uploadBegin`, `uploadChunk`, `uploadCommit`,
  `uploadAbort`). Paths are **host-wide absolute native paths** — there is no
  root confinement. Every call resolves to a parsed result object, never a
  rejected promise: `{ok:true, …}` or `{ok:false, error}`. `opts.host` is a host
  **id string**; omitted / `''` / `'local'` means this broker, and an unknown
  remote id **fails closed** with `{ok:false, error:'host_not_found'}` and makes
  no request.
- **`ctx.session`** — `procs(id, opts)`, the destructive `kill(id, pid, opts)`,
  and `git(id, opts)`. Resolves to `{status, json}` so you honour the broker's
  status contract verbatim — note that `409 {error:'session_gone'}` from `kill`
  is the session-destroy **success** path, not a failure.
- **`ctx.clipboard.observe(fn)`** — `fn(dir, text)` with `dir` `'out'` (copied
  out of a terminal) or `'in'` (pasted in), for every copy/paste core captures.
  Returns an unsubscribe fn and is *also* dropped on teardown, so capturing
  stops the moment the mod is disabled.

### Desktop, taskbar and menus

- **`ctx.taskbar.addStatusItem(node)`** — mount a node in the taskbar (before
  `#help-chip`). Auto-removed on teardown.
- **`ctx.taskbar.onItemsRendered(cb)`** — after every chip rebuild.
- **`ctx.taskbar.interceptActivate(fn)`** — `fn(key)` runs before core resolves
  a taskbar activation; return `true` if you changed what the desktop shows.
- **`ctx.desktop`** — `columnFilter(fn)`, `onColumnCreated(fn)`, `onPlaced(fn)`,
  `onForgotten(fn)`, `onReveal(fn)`, `onLayoutRender(fn)`. Each family is **one
  slot**: a second registration throws `ModConflictError`, so two mods can never
  silently fight over the desktop.
- **`ctx.registerKeyActions([{id,label,run}])`** — merges into the live action
  list every reader goes through (dispatcher, the Keyboard Shortcuts pane, the
  help corpus). A duplicate action id throws rather than last-wins.
- **`ctx.registerWindowMenuItems(fn)` / `ctx.registerDesktopMenuItems(fn)`** —
  register a callback; the callback returns an **array** of `renderMenu` items
  (separators included, so you own your own grouping — `renderMenu` does not
  collapse a stray one). The desktop callback runs in **both** window modes and
  receives `{ tiling }` saying which — check it rather than assuming tiling.
- **`ctx.windows.onTerminalCreate(cb)`** — subscribes to every terminal window,
  **replayed over those already open** and fired for future ones. `cb` gets
  `{ win, titleBar, host, wireId, addTitleBarItem, onDispose }`.

### Control Panel

- **`ctx.settings.boolean(key, def, opts)`** → checkbox
- **`ctx.settings.radio(key, options, opts)`** → radio group
- **`ctx.settings.select(key, options, opts)`** → `<select>`
- **`ctx.settings.combo(key, options, opts)`** → searchable `<input list>`
- **`ctx.settings.text(key, opts)`** → free text (the fifth primitive)

All five are read-through accessors onto the **shared `/state` settings blob**
(not namespaced localStorage), so a value syncs across your browsers, and all
five return `{get, set, onChange}`. `opts` carries `label`, `title`, `def`
(`boolean` takes its default as the second argument instead), `isBrowserGlobal`
(default true — hidden on remote-host tabs) and `mount` (`'mods'`, the default
per-host pane, or `'browser'`, beside the Hosts list). Reads are
**non-destructive**: nothing writes the default back.

`text` is the only primitive that is **not choice-constrained**, and that
distinction matters — `combo` *looks* free but treats its list as the legal
domain and refuses anything outside it. `text` takes `options` as
**suggestions** (a datalist), plus `placeholder`, `maxLength` (capped at
`MAX_MOD_TEXT_LEN`, 1024 UTF-16 code units) and `validate`. Its three rules:

1. `read()` is **structural only** — a bounded, control-char-free,
   well-formed string is returned as-is, even one this build's own validator
   would refuse, and it never writes. So an upgrade, a different engine or a
   stricter validator can no longer make a stored value evaporate.
2. `coerce()` gates what *we* write: String → strip control chars → drop
   unpaired surrogates → trim → cap.
3. `validate` is **write-only and its rejection is visible** — the rejected
   draft stays on screen next to a `.set-err` message and the stored value is
   untouched. It must be **synchronous**: a promise is truthy, so an async
   validator would accept everything, and the loader logs and rejects if you
   pass one.

Writes are debounced, and a pending one is flushed on teardown.
`mods/clock/clock.js` is the worked example: its time-zone box uses `text`
precisely because
`Intl.supportedValuesOf('timeZone')` is engine-dependent, and a `combo` refused
zones that the same engine would happily *render*.

- **`ctx.registerSettingsPane(spec)`** — a full custom section, for anything
  richer. `spec.render()` builds the DOM and is called **exactly once**;
  `spec.reflect(settings)` syncs it on Control Panel open **and on every
  `/state` convergence**, so it must be idempotent and must preserve an
  in-progress edit.

### Theme

```js
ctx.theme.get()        // -> {name, dark}
ctx.theme.vars()       // -> {'--bg': '#1e1e1e', …}
ctx.theme.onChange(fn) // -> unsubscribe fn
```

Derived from the **live DOM**, so it is right with the theme mod disabled,
absent or replaced.

- **`dark`** is the honest half: a YIQ test on the computed `--bg`.
- **`name`** is a *hint* — the synced `theme` key, reported only when an inline
  `--bg` on `documentElement` proves a theme mod actually applied one. Branch on
  `dark` when you need certainty.
- **`onChange(fn)`** fires **after** the new vars are on screen, only on a real
  change of `{name, dark}`, and is **not replayed** on register — call `get()`
  in your `init`. It is auto-dropped on teardown. A change that moves a public
  var without moving `name` or `dark` does **not** notify.
- **`vars()` is a separate call on purpose**: it costs a `getPropertyValue` per
  variable, which a subscriber that only branches on `dark` should not pay.

The public CSS-variable contract `vars()` resolves is `--bg`, `--bg-2`,
`--bg-3`, `--fg`, `--fg-dim`, `--accent-default`, `--sel-bg`, `--ok`, `--warn`,
`--danger`. Deliberately **not** in it: `--accent` (per-window, so there is no
document-level value — write `var(--accent, var(--accent-default))` inside a
window instead) and the geometry vars (`--taskbar-h`, `--title-h`,
`--handle-thick`, `--corner-size`). Everything else in the stylesheets is
private core.

**Values come back as the CSSOM reports a custom property — a substituted token
stream, not a resolved colour.** A theme mod writes six of these as plain hex,
but the four derived ones (`--sel-bg`, `--ok`, `--warn`, `--danger`) read back
as e.g. `color-mix(in srgb, #4aa3ff 28%, #1e1e1e)`. That is a valid CSS colour
string — assign it to a style property and it paints — but do **not** try to
parse a number out of it.

### Windows and Help

- **`ctx.registerWindowKind(spec)`** — register a brand-new app-window kind:

  ```js
  { appKind, factory(appData) -> win, serialize(win) -> record|null,
    restore?(record), retainOnClose?(record) -> bool,
    menu?: { label, launch(), iconKey?, iconGlyph?, closedItems?() } }
  ```

  It is the same registry core's built-ins use, so a mod kind is a first-class
  window everywhere the registry is consulted. A duplicate `appKind` throws
  (and `initMod` rolls the mod back); the kind is removed on teardown. An
  optional `restore` is called **directly**, so it does *not* get
  `openAppWindow`'s dedup-by-id — it must check `windows.get(record.id)` itself,
  because a lease-loss rebuild re-runs it.

  **The launcher icon.** `menu.iconKey` names an entry in core's `APP_ICON_SVG`
  table, which is **closed** — a key core does not ship resolves to nothing.
  `menu.iconGlyph` is the mod-owned alternative: a short **text/emoji** glyph
  that `renderMenu` paints with `textContent`. It is normalized by core's
  `appIconGlyph()`, which refuses an over-long raw string, strips control
  characters, bidi controls, invisible-payload carriers, lone surrogates and all
  whitespace, requires at least one substantive code point left over, and caps
  the result at `APP_GLYPH_MAX` code points — read that function for the exact
  rules rather than trusting this paragraph.

  There is deliberately **no `ctx.registerAppIcon(svg)`**: the API offers no way
  to put markup into the shared menu renderer, partly because the value a mod
  passes need not be its own (it might come from a `/mod-store` blob or a peer's
  `/state`). That is hygiene for this API — per §1 it constrains nothing a mod
  could do by reaching into the DOM itself.

  Two traps: an `iconKey` is not *owned* by anyone, so a mod can name a shipped
  key and get that icon — only the **label** identifies a launcher; and
  `iconKey` takes **precedence** over `iconGlyph`. Declare one or the other, and
  do not rely on the fallback for a key core does not currently ship.

- **`ctx.registerHelpCards(cards)`** — contribute typed Help cards. **Never raw
  HTML**: `{ slug, section, title, body:[block], keys?, search? }` where
  `block = {t:'p'|'bullet'|'sub', spans:[span]}` and
  `span = {t:'text'|'strong'|'code'|'kbd', v:String}`. Unknown types degrade to
  text; values are coerced to `String`. Removed on teardown, and an open Help
  window re-renders.

---

## 9. Help: `help.md` and the regen you must not skip

A mod may ship a `help.md` beside its `mod.json`, in the same wiki Markdown the
`wiki/` pages use. `help_corpus.build_mod_sections()` parses every mod dir that
has **both** files, turns it into a Help section tagged with the owning mod id
(so the frontend hides it while the mod is disabled), and
`build_full_corpus()` merges it with the wiki corpus, sorted by
`(order, slug)`. A slug that collides — with another mod or with a wiki page —
is a hard `BuildError`.

`webterm/broker/help_corpus.json` is the **packaged, tooling-generated**
fallback, and it bakes the *shipped* mod sections in. It is never hand-edited.

An **installed** mod's `help.md` is captured into the broker's index at install
or scan time and layered onto the served corpus at **serve time only** — never
into the packaged JSON, which is why the regenerator's output does not depend on
what happens to be installed on your machine. It is also the one part of an
install that *does* appear without a page reload.

> **If you add, remove or edit any `help.md` (or the `help` block in a
> `mod.json`), you must run:**
>
> ```
> python -m webterm.broker.help_corpus
> ```

`tests/test_help_corpus.py::test_packaged_json_in_sync_with_wiki` regenerates
the corpus and asserts a **byte-exact** match against the checked-in file. It
fails on the smallest drift, and the fix is always to run the regenerator, never
to edit the test.

`help.md` is optional. Skip it when the UI already explains the whole feature —
a mod that *is* one Control Panel control has nothing a Help page would add.
That is an authoring judgement, not a rule.

---

## 10. Publishing an installed mod

### 10.1 The `x-` namespace rule

**An id is reserved for first-party (shipped) mods iff it does *not* start with
`x-`; an installed mod's id *must*.**

The id *shape* is the same on both sides — `_MODSTORE_ID_RE` in `app.py` and
`MOD_ID_RE` in the loader, both `[a-z0-9][a-z0-9-]{0,63}` — and `x-notes`
already matched it before the rule existed. That is the point: the rule is a
prefix convention layered on top, so **no key a shipped mod owns moves, and
neither validator has to change.**

What the one-character prefix buys is that a *single lexical test* separates all
five namespaces a mod id keys, because all five derive from the same string:

1. `/mod-store/<id>` — the durable per-mod server store;
2. the per-broker mod-policy pin map (`webterm_mod_policy.json`, `POST /mods/policy`);
3. the `webterm:mod:<id>:` localStorage prefix behind `ctx.storage`;
4. `webterm:mods:disabled`, the per-browser enable-override set;
5. the catalog — `ui._MODS` / `mods/<id>/` on one side, `<mods_dir>/<id>/` on
   the other.

It is enforced in three places, and a change to the rule has to update all
three: the install validator (`400 reserved_id`), the scanner (a non-`x-`
directory in `mods_dir` is skipped with a loud log, so a hand-dropped `clock/`
cannot shadow the shipped `clock`), and CI (no shipped id starts with `x-`, no
shipped `requires` names an `x-` id).

**`x-<author>-<name>` (e.g. `x-johnconnornpc-notes`) is a documented
convention, not an enforced field.** Nothing validates the middle segment and
there is no `vendor` key in the manifest. Carrying a real scope component in the
id would mean widening both validators, the policy sanitiser and the mod-store
key filter in lockstep — four places to keep synchronized, for a component
nothing would enforce anyway.

**Not covered, plainly:** DOM ids a mod writes in its own markup. The
loader-generated ids are built from the mod id and so are namespaced; a mod that
writes `id="clock-chip"` itself is not. That is authoring convention, exactly as
it already is for shipped mods.

**There is no automatic migration of pre-existing `x-` state.** `/mod-store` and
the pin map already accept any id-shaped key, so a browser can hold
`/mod-store/x-notes`, a pin for `x-notes`, and `webterm:mod:x-notes:*` before
anything called `x-notes` was ever installed. Installing it adopts all of it.
The install response says so — `adopts_existing_state: {mod_store, pin}` — for
the server-side half; localStorage cannot be inspected server-side and is not
covered.

### 10.2 The portable-mod contract

An installed mod is loaded as a **separate classic `<script src>`**, not as an
`import()` and not spliced into the bundle. Classic (not module) is deliberate:
a module would impose module scope plus strict mode and would not publish its
top-level function declarations. CSP `'self'` authorises both identically, so
this is a source-mobility decision, not a security one.

**A spliced fragment and a separately-loaded classic script are not
equivalent.** The real differences:

| | spliced into the bundle | loaded as `<script src>` |
|---|---|---|
| `"use strict"` at the top | a plain string expression — **inert** | a **directive** — the whole file is strict |
| declaration instantiation | all of the inline script's declarations exist before *any* of its statements run, so your top-level function is visible to code earlier in the bundle | your declarations do not exist until the file is fetched **and executed** |
| execution timing | page-script eval, before `loadMods()` | after `/info`, asynchronously, possibly after boot |
| `document.currentScript` | the one inline `<script>` (no package stamp) | the injected element, carrying `dataset.modPackage` |
| a literal `</script>` | **fatal** — closes the element, breaks the page | harmless |
| a `const`/`let`/`class` name collision | a compile error for the **whole bundle** — blank desktop | a compile error for **that script only** — one dead mod |

So the contract, if you want source that moves in both directions unchanged:

1. **Nothing at top level *runs* except the `registerMod({…})` call.** No DOM
   writes, no fetches, no timers, no reads of desktop state — all work in
   `init(ctx)`. Plain declarations are fine (see rule 5); side effects are not.
2. **No top-level `"use strict"` directive.**
3. **Nothing outside your package may depend on your top-level names.** A
   top-level *call into* your functions from another fragment cannot work: as an
   installed package, your declarations do not exist when the bundle evaluates.
   Cross-mod calls at *runtime* are fine, but only once the provider is
   **active** — declare it in `requires` and do not assume every package has
   executed by the time boot finishes (the load deadline explicitly lets boot
   proceed with a script still in flight). This is why some shipped mods are not
   portable as-is: core's app-window store calls `openNoteOrEditorWindow`, which
   the `editor` mod declares at top level.
4. **No literal `</script>`.**
5. **Prefix every top-level `const` / `let` / `function` name.** In-tree a
   collision with core kills the page; out of tree it kills your mod.

Rules 1, 2 and 4 are enforced by the portable-mod lint in
`tests/test_ui_assets.py`, and every shipped mod passes them. Rule 3's known
in-tree exceptions are pinned there too, as an exact set of seven edges:

| top-level name (owner) | reached from |
|---|---|
| `openNoteOrEditorWindow` (`editor`) | core `54_js_app_windows_store.js`, `sticky` |
| `loadCodeMirror` (`editor`) | `scratchpad` |
| `applyPattern` (`pattern`) | `theme` |
| `findHelpWindow`, `refreshHelpCorpus` (`help`) | core `86_js_mod_loader.js` |
| `toggleHelpWindow` (`help`) | core `78_js_keybindings.js` |

The set is a drift guard **both ways** — a stale edge fails as loudly as a new
one — so decoupling a mod forces it to shrink. It did: #177 retired `agent-docs`
(see `webterm/broker/mods-deprecated/README.md`), which took two edges with it,
`editorFile` (`editor`) ← `agent-docs` and `openAgentDocsWindow` (`agent-docs`)
← `editor`. The surviving call site in `editor.js` is a
`typeof openAgentDocsWindow === 'function'` guard, which owns no shipped name
and so is not an edge.

So `editor`, `help` and `pattern` cannot be republished as
installable packages unchanged. **Rule 5 is not enforced** — check it yourself.
The lint is a floor, not a proof: it reads source text, so it cannot see a
call hidden inside `registerMod`'s own argument.

### 10.3 Installing takes effect on the next page load — always

`POST /mods/install`, `/mods/uninstall` and `/mods/rescan` change what the
broker serves **immediately** — the catalog, the asset routes and the Help
corpus all swap at once — but they change **nothing in an already-open
desktop**. Install and uninstall say so in their own response, with
`"applies": "next_page_load"`.

This is **forced, not unimplemented.** JavaScript global lexical bindings cannot
be removed. A mod whose top level says `const DB = …` cannot be re-executed in
the same page: the second execution dies with
`SyntaxError: Identifier 'DB' has already been declared`, and a `var`-based mod
instead hits `registerMod`'s duplicate-id throw. `_takeDown()` is a *teardown*,
not an unloader — it reverses `init`, it does not un-execute a script. So there
is no live replace, no live-uninstall teardown, and no discovery of an install
another client performed. That is the same contract the per-broker mod pins
already ship.

What *is* delivered is what the feature was for: **no process restart** and no
source edit. Drop a mod into a live broker, reload the tab, and it is there.

Related loader behaviour worth knowing:

- **Packages load in parallel; the scripts *within* one package load in
  manifest order, one after the next.** Across packages, order is irrelevant
  because the topological sort runs afterwards, and document-ordered async would
  let one slow file head-of-line-block every other mod. Within a package the
  topo sort says nothing at all — it orders mod *declarations*, not the files of
  one mod — so `scripts` is honoured as an ordered list. **A multi-script
  package must call `registerMod` from its LAST script**: a registration is
  acted on the moment it arrives, so registering from the first file would let
  the mod init before its siblings had loaded.
- **Stylesheets are injected but not awaited**, and are live regardless of
  enable, pin or init — deliberately the same posture as a shipped mod's CSS.
  A teardown cannot remove them.
- Each `(id, gen, file)` URL is fetched **at most once per page load**, through
  a shared in-flight promise map, so the post-login retry cannot double-execute
  a package.
- `MOD_SCRIPT_TIMEOUT_MS` is a **proceed-anyway deadline, not a cancel** —
  nothing can cancel an in-flight `<script>`. A package that lands later still
  executes, and `_lateRegister` brings it up if this page requested it.
- `registerMod` binds a declaration to the package whose script is running
  (`document.currentScript.dataset.modPackage`) and refuses a declaration whose
  id is not that package's, with a `wrong-id` row. This is a **correctness
  convention, not a boundary**: while a stamped package script runs
  synchronously, a declaration for a different id is refused instead of silently
  colliding with (say) the shipped `clock`. A package that registers from a
  promise or timeout has `currentScript === null` and is simply unattributable,
  at which point the duplicate-id `ModConflictError` is the only backstop.
- **`?nomods=1`** on the page URL makes `loadMods()` return before it fetches or
  inits anything. It is the rescue hatch for a mod that bricks the desktop and
  therefore makes the Control Panel — where you would uninstall it —
  unreachable. It is read-only and deliberately *not* scrubbed from the URL the
  way `?token=` is, because replaceState-ing it away would mean a reload
  silently re-enabling the mod you are there to remove.

### 10.4 An installed mod's code and styles are public

`GET /mods/<modId>/<gen>/<name>` is **public**, like `GET /` and `/vendor/*`,
and that is forced rather than chosen: a `<script src>` cannot carry an
`Authorization` header, `?token=` is structurally banned, and a `fetch`+`blob:`
workaround would need `blob:` in `script-src`. The posture is the existing one —
`GET /` is public and already carries every shipped mod's source.

So **an installed mod's source and its stylesheets are readable without a
token** — by anyone who knows the id *and* the generation *and* the file name.
**Do not put a secret in a mod** — not in its code and not in its stylesheet.

`help.md` is a different matter: only `.js`/`.css` are servable, so this route
cannot hand it out at all. Its only surface is `/help-corpus.json`, which is
public for the same bootstrap reason `GET /` is (the Help window has to render
on the login page) — but since #173 it serves the installed mods' help sections
**only to a caller holding the token**. Without one it is the wiki +
shipped-mod corpus alone, so installed ids, help text and manifest label/icon
are not enumerable. Treat that as *not published*, not as *secret storage*: it
is one token away, and a mod's help is written to be read.

**The generation is not a second secret.** `<gen>` is a content hash of the
package, so anyone holding the exact bytes of a *publicly distributed* mod
recomputes it and can ask this broker for the file: a `200` rather than a `404`
confirms that mod is installed. So #173 removed **enumeration** — the list of
installed ids, and the help text with it — not every last bit about a mod
someone already knows to ask for. A mod whose bytes were never published stays
unguessable in both segments.

### 10.5 The install API

```jsonc
POST /mods/install          // browser auth token; NOT lease-gated
{ "manifest": { "id": "x-notes", "version": "1.0.0", "ctxVersion": 1,
                "title": "Notes", "description": "…",
                "scripts": ["notes.js"], "styles": ["notes.css"],
                "requires": ["editor"], "tiers": ["settings"],
                "permissions": ["file"],
                "help": { "label": "Notes", "icon": "📓", "order": 2100 } },
  "files": { "notes.js": "…", "notes.css": "…", "help.md": "…" },
  "replace": false }

POST /mods/uninstall   {"id": "x-notes", "purge": false}
POST /mods/rescan      {}
GET  /mods/installed                    // operator detail for the Control Panel
```

All four are browser-realm token-gated, `serve_ui`-gated (a headless broker
registers none of them), and deliberately **not** lease-gated — this is broker
configuration, and the point is being able to administer a broker somebody else
is using.

**The payload is JSON with text files, deliberately not an archive.** No zip
parser means no zip-slip, no zip bomb, no symlink entries. Mods are text; binary
rides `data:` URIs. `mod.json` is **written by the broker** from the canonical
manifest and is a reserved key in `files`, so a payload cannot ship a manifest
that disagrees with what was validated.

Differences from a shipped manifest, all enforced:

- `scripts` is **required** and is an ordered non-empty list of `.js` names, all
  present in `files`. `entry` is not accepted (`unknown_manifest_key`).
- `id` must pass the id regex (`bad_mod_id` otherwise) **and** start with `x-`
  (`reserved_id` otherwise).
- **Unknown keys are rejected**, not ignored.
- `help.slug` is accepted and dropped — an installed section's slug is **forced
  to the mod id**, which is `x-`-prefixed, and CI pins that no wiki page stem
  and no shipped mod's `help.slug` uses that prefix. The merge still checks for
  a collision and drops the installed section if one somehow appears, because
  merging installed help must never be able to blank the Help window.
- `defaultEnabled` is accepted and ignored; the catalog always reports
  `default_enabled: false`. Install and enable are separate steps, which matches
  how the shipped default-off mods already behave. **This is not containment:**
  an installed package's scripts are fetched and executed on every page load
  whatever its enabled state — being off only means `init()` is not called, and
  its stylesheets are live either way.
- `permissions` is checked against the source text, not merely displayed — the
  install-time capability lint (#193), covered on its own below.
- Filenames must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` with a `.js`/`.css`/`.md`
  suffix, must avoid `?`, `#`, `%` and `:` (an NTFS alternate data stream:
  `base.css:payload.js` passes a naive bare-name test), must not be a Windows
  device stem (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`), and must not
  collide with another name under `casefold()`.
- Every file must be valid UTF-8, **BOM-free**, **newline-terminated**, and
  within `ui._MAX_LINES` lines — the *same* rule set the spliced fragments obey,
  imported from `ui` rather than retyped.
- A `.css` file may not reference an external origin: `@import` and absolute
  `url(http…)` / `url(//…)` are rejected (`css_external_reference`). The app's
  CSP sets only `script-src` and `frame-ancestors`, so a stylesheet would
  otherwise be a silent egress channel. **Defence in depth, not a boundary** —
  the mod's own JS can `fetch()` anything.
- Caps, at the time of writing: 32 mods per broker, 32 files per mod, 256 KiB
  per file, 512 KiB per mod, 2 MiB request body (64 KiB for the uninstall and
  rescan bodies). The constants in `modinstall.py` are authoritative, and
  `GET /mods/installed` reports the live values under `limits` — read them from
  there rather than from here.

Every refusal carries a distinct code, because "your mod was rejected" with no
reason is a support ticket: `too_large` · `bad_json` · `bad_mod_id` ·
`bad_generation` · `reserved_id` · `id_in_use` (409) · `not_installed` (404) ·
`bad_file_name` · `reserved_file_name` · `too_many_files` · `file_too_large` ·
`total_too_large` · `bad_encoding` · `bad_scripts` · `bad_styles` ·
`bad_requires` · `bad_manifest_field` · `unknown_manifest_key` ·
`css_external_reference` · `undeclared_capability` · `too_many_mods` (409) ·
`write_failed` (500).
`modinstall.ERROR_STATUS` is the authoritative map; a client must tolerate a
code it has not seen.

**The `permissions` capability lint (#193).** Unlike `tiers` (§5), which is a
self-reported claim nothing checks, `permissions` is a **closed vocabulary**
(`modinstall.PERMISSIONS`), and `validate_package` scans every `.js` file the
package ships for direct, syntactic use of the capability each name covers:

| permission | what it covers |
|---|---|
| `clipboard` | `ctx.clipboard` |
| `egress` | `fetch(` or `hostFetch(` |
| `file` | `ctx.file` |
| `remote-admin` | one of the broker's admin route strings, named literally — at the time of writing `/mods/install`, `/mods/uninstall`, `/mods/rescan`, `/mods/policy`, `/update/policy` (built from `app.ADMIN_ROUTES`, so a new admin route becomes `remote-admin` automatically, with no second list to keep in sync) |
| `session` | `ctx.session` |

A capability the source uses but `permissions` did not declare is refused with
`undeclared_capability`, naming the **first** offender by file (sorted) then
by position within it — e.g. `notes.js:12 uses 'ctx.file', which needs permissions: ["file"] in the manifest` as the response's `detail` text. **Declaring
more than you use is fine**; only a used-but-undeclared capability is refused.

**Absent and `[]` are not the same declaration.** No `permissions` key at all
means "written before this lint existed" — an already-installed generation
from before #193 is grandfathered past the check (see `/mods/rescan` below)
and keeps serving. `permissions: []` is a **positive claim** — "this package's
source reaches none of the five" — and is checked exactly like any other
declaration. A new package should always declare truthfully, writing `[]`
when it genuinely uses none of the five; there is no reason for a *new*
manifest to omit the key.

A mention inside a **comment** never counts, for any permission — the scanner
blanks `//` and `/* */` comments (and regex literals) before it looks for
anything, the same `blank_js_literals` machinery the portable-mod lint (§10.2)
uses. For `clipboard` / `egress` / `file` / `session`, a mention inside a
**string literal** doesn't count either — those four are matched against
source with string bodies blanked too, so `"call ctx.file to persist"` trips
nothing. `remote-admin` is the one exception: its evidence *is* a literal — a
mod naming `/mods/policy` inside a `fetch()` call — so that one check alone
keeps string bodies intact and blanks only comments and regex literals. A
route named in an actual string is a real use; the same text sitting in a
`//` comment still is not.

The rule runs on **both doors**: the same `validate_package` call underlies
`POST /mods/install` and the `mods_dir` scanner (`POST /mods/rescan` and
every boot), so a package dropped by hand faces exactly the check the wire
does — see the grandfathering note under `/mods/rescan` below for the one
carve-out (an already-installed generation).

**Generations.** A mod's assets live at `<mods_dir>/<id>/<gen>/`, where `gen` is
a sha256 over the canonical manifest bytes plus the sorted `(name, sha256)`
pairs. Because that hash is **in the URL**, a replacement can never be served
under the old URL — `Cache-Control: immutable` is honest and SRI has something
stable to pin. `<id>/CURRENT` is the atomic commit pointer, and
`RETAINED_GENERATIONS` older generations are kept so a page that started booting
against one survives a replacement mid-flight; an old enough generation is
eventually swept and then 404s.

**Uninstall** is *not* idempotent at the HTTP level: a retry after a lost
response answers `404 not_installed` even though the first attempt succeeded.
Catching a typo was judged worth that ambiguity — treat "404 after a retry" as
possible success. `purge: true` additionally clears this broker's
`/mod-store/<id>` and its pin, in a fixed lock order and **data-first,
code-last** so a crash leaves code without data rather than data without code.
It cannot touch what other browsers hold in `localStorage`.

**`POST /mods/rescan`** re-reads `mods_dir`. It is an operator convenience and
**not a trust boundary**: it treats anyone who can write `mods_dir` as
authorized to supply code the broker will serve and the desktop will execute
with full authority. Keep that directory as protected as the broker's own
configuration. What the scanner does owe is coherence: it refuses
symlinks and reparse points, refuses anything whose realpath leaves `mods_dir`,
refuses a non-`x-` directory, and validates every byte it captured under exactly
the same rules an install obeys — **including the `permissions` lint above**, so
a hand-dropped package cannot dodge it by skipping `POST /mods/install`
entirely. The one exception is a generation this store **already installed**
(its own `.gen.json` names this directory and lists exactly the files in it):
that generation is grandfathered past the lint, so it keeps serving — across a
rescan and a restart — even with `permissions` absent, rather than going dark
retroactively the day the check shipped. Replacing or reinstalling it, through
either door, requires the declaration like any new package. The destructive
sweep refuses to run at all in a directory without the `.browserland-mods`
marker file. Unpacking an archive into a live `mods_dir` is not the recommended
path; use the API, or stop the broker first.

### 10.6 Status vocabulary

**Control Panel → Mods** is where an operator lives with all of this: it lists
the union of catalog packages and registered declarations, joined on id, badges
each row `shipped` or `installed`, and carries the **Install a mod…** and
**Uninstall** actions. The union matters — cycle rows, 404s and SRI mismatches
never call `registerMod`, so a pane driven off the registration list simply
could not show them.

Each row's `state` is one of the following. The vocabulary is produced by the
loader's status model, so treat an unfamiliar value defensively rather than
assuming this list is complete:

`active` · `off` · `blocked` (enabled, but a `requires` is not active) ·
`cycle` · `blocked-by-cycle` · `failed` (deps satisfied, `init()` threw) ·
`fetch-failed` (404, transport failure, **or SRI mismatch**) · `timeout` ·
`loading` · `no-register` (the script loaded but never called `registerMod` —
the compile-error case, which still fires `load` on the element) · `wrong-id`.

---

## 11. Traps that have actually bitten this codebase

**TDZ inside `init()` disables the whole mod.** If a hoisted `function` is
*invoked* before a `const`/`let` it reads has been initialized — typically
because the declaration sits further down the same scope — the read throws a
`ReferenceError` ("Cannot access X before initialization"), `initMod` catches
it, and **the entire mod is disabled**, not the one feature. It has happened
more than once. Declare mutable state *before* anything that could call the
closures reading it, and prefer function-local declarations over fragment-level
ones that a hoisted function reads.

**CI never executes UI JavaScript.** The Python suite asserts *source text* and
served bytes. A TDZ error, a typo in a callback or a wrong property name is
invisible to `pytest` and only appears in a real browser. Load the page and
check the console.

**Register `ctx.onUnload` before anything that can throw.** If `init` throws
after allocating a timer but before registering its teardown, `initMod`'s
rollback runs an `unloads` list that never learned about it — the mod is
disabled and the timer runs forever.

**A fragment edit needs a broker restart.** See §7. The page is assembled at
import.

**`prefs`/settings values sync.** A `ctx.settings.*` key lives in the shared
`/state` blob, so it is *not* browser-local even if it feels like a local
preference. Use `ctx.storage` for genuinely per-browser state, and
`isBrowserGlobal` only controls which Control Panel tab shows the widget.

**Reflect must be idempotent.** `spec.reflect` on a settings pane runs on every
`/state` convergence, not just on open. Rebuilding your DOM there destroys an
in-progress edit.

**`renderMenu` never collapses separators.** If you contribute menu items
conditionally, contribute your separator conditionally too, or you get a stray
rule.

**A per-mod enable is per-browser; a pin is per-broker.** A broker pin outranks
the per-browser set, which in turn outranks your `defaultEnabled`, and a pinned
mod refuses `setModEnabled` outright. Your default is a starting point, not a
promise about what any given browser is running.

---

## 12. Checklist for a new shipped mod

- [ ] `webterm/broker/mods/<id>/mod.json` — `id` matching the directory *and*
      the `registerMod` call, plus `version`, `ctxVersion`, `title`,
      `description`.
- [ ] `webterm/broker/mods/<id>/<id>.js` — one top-level `registerMod({…})`,
      prefixed top-level names, no `</script>`, everything in `init(ctx)`,
      teardown registered first.
- [ ] `tiers` declared honestly in `registerMod` (§5).
- [ ] `defaultEnabled` and `requires` **identical** in `mod.json` and
      `registerMod` — a test asserts both directions.
- [ ] Path appended to `ui._MODS`, **after** anything it `requires`.
- [ ] Any `.css` listed in `styles`, UTF-8, no BOM, newline-terminated, within
      `ui._MAX_LINES`, with **prefixed** selectors (§13).
- [ ] Optional `help.md` + the `help` block — then run
      `python -m webterm.broker.help_corpus`.
- [ ] `python -m pytest tests -q` clean, and the page loaded in a real browser
      with the console open.
- [ ] Restart the broker before you believe anything you see.

---

## 13. CSS: what you own, what you may use, what is private

**Prefix everything you own.** A mod's stylesheet is concatenated into the same
`<style>` zone as core's, so an unprefixed `.row` or `.item` is a page-wide
selector. Use `<id>-…` or a class on your own root node.

**Contract surface** — shared primitives you are expected to reuse rather than
restyle:

- `.set-*` — the Control Panel primitives (`.set-section`, `.set-row`,
  `.set-check`, `.set-title`, `.set-hint`, `.set-err`, `.set-browser-global`),
  which the `ctx.settings.*` primitives emit for you.
- `.app-toolbar` and its children (`.app-host-btn`, `.app-file-name`,
  `.app-dirty`) — the app-window toolbar chrome.
- `.tb-btn` — title-bar buttons.
- `.app-tab`, `.app-tabs`, `.app-tab-dot` — in-window tab strips.
- `.app-dialog-*` — the shared dialog primitives behind `openDialog`,
  `openTextPrompt`, `openConfirmDialog`, `openInfoModal`.
- The Help card primitives, for anything rendered into the Help window.

**Private core** — do not target, do not depend on: everything in
`13_css_tiling.css` and `14_css_dragdrop.css` (the tiling strip, gutters, drop
zones), and any selector not listed above. It changes without notice.

**Known debt, not a pattern to copy.** Some mods that were *extracted* from core
still leave their stylesheets behind in it — at the time of writing `editor`,
`file-manager`, `task-manager` and `sticky` (`.app-editor*`, `.app-fm*`,
`.tm-*`, `.app-note` in `11_css_apps.css`). That is migration debt to unwind,
not an example: a new mod ships its own stylesheet through the manifest's
`styles`.

Prefer the theme variables (§8) over hardcoded colours: `--bg`, `--bg-2`,
`--bg-3`, `--fg`, `--fg-dim`, `--accent-default`, `--sel-bg`, `--ok`, `--warn`,
`--danger`, and `var(--accent, var(--accent-default))` inside a window.
