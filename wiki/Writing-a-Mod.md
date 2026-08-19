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

Everything in §3–§10 applies to both. §11 is the extra contract an installed mod
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
  "permissions": ["file"],          // closed vocabulary; checked only via the install door — see §11.5
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
  §11.5.
- **`help`** — `slug` / `label` / `order` / `icon` for the in-app Help section
  (§10). All optional; slug defaults to the mod id, label to `title`, order to a
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
  (`x-<author>-<name>`), not this field — see §11.
- Anything else — ignored by the shipped-mod readers, and **rejected** by the
  install validator (`unknown_manifest_key`), so a typo is loud rather than
  silent.

Note what is *not* in `mod.json`: **`tiers`**. It lives only in the
`registerMod` call, and `ui.mod_catalog()` deliberately does not report it.

---

## 5. `tiers` and `needs` — declared vs. gated

Two similarly-shaped declarations answer two different questions. `tiers` says
what a mod *claims* to touch, and nothing checks it. `needs` says what ctx
surface a mod *cannot run without*, and the loader refuses to `init` it if
that surface is missing. Read them together — they are opposites, not
variants of the same idea.

### `tiers` — declared, not enforced

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

### `needs` — a presence gate (#197)

```js
needs: ['file', 'windows.onTerminalCreate'],
```

`needs` is an array of ctx-surface names this mod cannot function without — a
bare member name (`'file'`) or a **dotted path** into one (`'windows.onTerminalCreate'`).
Unlike `tiers`, it is checked: once `initMod` starts building this mod's `ctx`,
the loader resolves every entry against that live object — **own** properties
only at each segment, so a mod never "has" `toString` or `constructor` just
because the prototype does, and a member that throws on access counts as
absent too. An unmet entry **blocks init outright**: nothing partially
initializes, the claimed slot is released, and the Mods pane row reads
**`blocked (needs windows.onTerminalCreate)`** — naming every unmet entry —
instead of showing the mod as active while it quietly does nothing, which is
the failure mode `mods/git/git.js` guards against today with a plain
`if (!ctx.windows) return;` rather than a declared, pane-visible reason.

That label is a different thing from a `requires`-blocked row's `needs: editor`
text (§6) even though both use the English word "needs": `requires` names
other **mod ids** and is checked first, before a `ctx` is even built; `needs`
names **ctx surface** and is checked once `init` is actually about to run. A
mod that fails both is reported on the `requires` refusal, since that is the
one the loader reaches first.

A few things worth being precise about:

- **A pin does not override it.** A pin is policy — should this mod be on;
  `needs` is a fact about what this build's `ctx` offers. A pinned-on mod with
  an unmet need still reads `blocked (needs …)`; pinning it cannot conjure the
  missing surface.
- **An older loader, or a page built without the ctx-extender registry,
  silently drops the field.** `needs` is normalized the same way `tiers` and
  `requires` are, by a companion fragment (`86c_js_mod_ctx_ext.js`) that a
  given page may not carry; where it is absent, the gate simply never runs and
  the mod inits exactly as it would have before `needs` existed — absence is
  never an error (#157). Keep your own `typeof ctx.x` guard as the second line
  of defence regardless; `needs` makes the degradation *visible*, it does not
  replace it.
- **`needs` is not `requires` and not `permissions`.** `requires` cascades
  through pins and take-downs (§6); `permissions` is an install-time,
  server-side review lint over source text (§11.5). `needs` is client-side,
  checked once at init against the live `ctx` — it asks what a mod can *run
  against here*, not what other mods must be active or what a reviewer
  permits.
- **`ctx.capabilities` (§8) is the companion read** — a mod can inspect what a
  build offers instead of only declaring what it requires.

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
- **A shipped mod can never meet a loader other than the one it shipped beside.**
  `ui.assemble()` puts every `_MODS` path into the *same* string as
  `86_js_mod_loader.js` and its `86*` companions, and that one string is what
  the broker serves. There is no path by which an in-tree mod script is
  delivered next to some other build's loader, so for a shipped mod "does this
  build's `ctx` have X" is settled at the commit rather than at runtime — which
  is what makes a feature-detect fallback in an in-tree mod dead code for
  anything already in `ctx` at that commit (see `ctx.visibility`, §8).
  **An installed package is the opposite case** (§11.2): it is fetched as its
  own `<script src>` by whichever broker it was installed on, and that broker
  may be older than the package, so a portable mod keeps its detection or
  declares `needs` (§5).

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
- **`ctx.visibility`** — `pausableInterval(fn, ms)` → a `{stop}` handle, and
  `onVisibility(fn)` → an unsubscribe. The interval slows to 10× `ms` while the
  tab is hidden and coalesces the runs it skipped into exactly one catch-up when
  the tab comes back (including after a bfcache restore, where no tick fired at
  all). Both fail closed once the mod is tearing down, and both register their
  own teardown, so neither can outlive a disable.

  **For a shipped mod this is a floor, not a maybe.** It is a plain member of
  the object `makeCtx` builds — not one an extender adds afterwards — so if your
  `init(ctx)` is running at all, `ctx.visibility` is on that `ctx`; and per §7
  an in-tree mod is served beside the very loader that put it there. A
  `ctx.visibility ? … : setInterval(…)` fallback in a shipped mod is therefore
  unreachable code. The ones still carrying such a fallback — `mods/aistatus/`,
  `mods/clock/`, `mods/git/`, `mods/task-manager/` and `mods/update/` — are a
  migration of their own, not something to copy from.

  **The floor stops at the repo.** An installed package is loaded by whatever
  broker it was installed on (§7, §11.2), and that loader really may predate
  `ctx.visibility` — so portable source keeps the feature detect, or declares
  `needs: ['visibility']` (§5) and blocks visibly instead.
- **`ctx.signal`** (#198) — one `AbortSignal` per **activation**, aborted at the
  head of teardown. Pass it explicitly to the calls you want cancelled; it has
  its own section below.
- **`ctx.capabilities`** (#197) — a **frozen**, per-mod map, `{family:
  <integer version>}`, of the `ctx` surface this build's loader actually hands
  you — e.g. `{file: 1, serverStore: 1, windows: 1, settings: 1, theme: 1, …}`.
  It exists so a mod can ask "does this build offer X" directly instead of
  sniffing loader-private names the way `mod-sync` currently does (`typeof
  _pin`, `typeof _modTextOk`). Additive on `ctxVersion` 1 like everything else
  on this page — feature-detect it (`ctx.capabilities &&
  ctx.capabilities.theme`) before reading it on a build old enough to predate
  it. The map is built lazily on first read and then frozen: your copy cannot
  be mutated into lying to you, and mutating it never reaches another mod's
  copy or the `needs` gate (§5) any mod is checked against. It is **observed,
  not promised** — a family whose own ctx extender threw during setup is
  simply absent from the map, exactly as it is absent from `ctx` itself, so
  the two can never disagree.
- **`ctx.events.on(name, fn)`** (#195) — subscribe to the core → mod event bus.
  Returns an unsubscribe fn, which also rides teardown. Five declared events,
  two of which replay their current state to a late subscriber; the table and
  its rules are the contract, so read them below before you use it.

### Storage

- **`ctx.storage`** — `get(key)` / `set(key, value)` / `remove(key)` over
  `localStorage`, namespaced `webterm:mod:<id>:<key>`. **Per-browser**, string
  values, never synced. Every call swallows storage failures (private mode,
  quota) and `get` returns `null`.
- **`ctx.prefs`** (#196) — the *sanctioned* browser-local tier: `get(key,
  default)` / `set(key, value)` / `remove(key)` over its own
  `webterm:modprefs:<id>:<key>` namespace, with **JSON** values rather than
  strings. A **sibling** of `ctx.storage`, not a corner of it — the namespace is
  the whole contract, and it is what makes these records survive "Reset local
  view" and structurally unable to ride `/state`. See below.
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

  Two members are built **on top of** `get`/`set` and have their own
  subsections below: **`update(fn, opts)`** (#196), the compare-and-swap that
  re-applies your reducer over the winner of a `409`, and
  **`saveChain(opts)`** (#196), the debounced, single-in-flight save pipeline
  built on `update`. Use them. Three mods hand-rolled a compare-and-swap
  before these existed, and the rebase that is easiest to write by hand — adopt
  the winner's `rev`, re-`PUT` the object you already had — is the one that
  erases every key another browser changed in between (#158; it is why
  `mods/mod-sync/` deliberately does not use core's `putHostState`).

### Host I/O

- **`ctx.http.fetch(hostId, path, opts)`** (#200) — the sanctioned
  authenticated request to any broker, resolving to `{status, json?|text?,
  error?}` and never rejecting. `hostId` is **required** (a missing one throws
  synchronously — there is no same-origin default) and `timeoutMs` is a
  **total** deadline including the body read, unlike raw `hostFetch`. See
  below.
- **`ctx.hosts.list()`** (#200) — a frozen `[{id, label, url, brokerId, self}]`
  snapshot, no token. `brokerId` is `null` until the host has answered over
  `/info`. See below.
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
- **`ctx.hosts.invalidate(id)`** (#195) — "forget everything this page knows
  about host `<id>`": clears every registered per-host cache and repaints the
  host surfaces, returning a boolean. It notifies nobody and saves nothing —
  see below.

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
  `{ win, titleBar, host, wireId, addTitleBarItem, onDispose, onModTeardown }`
  — one subscription's replay and its create-time emit are mutually exclusive,
  so each window reaches your callback exactly once and you need no
  decorate-once `WeakSet`. `onModTeardown` (#195) is the per-window disposer;
  it has its own section below.

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

**A malformed options list no longer kills the mod (#203).** `radio` /
`select` / `combo` throwing on an empty, invalid or duplicate-valued options
list used to disable init, and with it the *whole* mod — a single bad
computed list nuking a mod's every other feature was disproportionate, and
`mods/clock/clock.js` shipped its own defensive `zones` dedup for exactly
this reason (still there today; not removed by this change). Instead the
accessor comes back **degraded**:

```js
const s = ctx.settings.select('k', badOptions);
s.ok            // false — true on a healthy control, ABSENT on an older loader
s.error         // 'invalid_options' | 'duplicate_option' | 'async_validator'
s.get()         // the coerced def, or '' — never throws
s.set(v)        // no-op, the family's existing silent-drop idiom
s.onChange(fn)  // accepted, never fires
```

**Feature-detect `s.ok === false`, never `!s.ok`.** `ok` is a #203 addition —
a loader that predates it has no `ok` field at all, so `!s.ok` reads every
healthy accessor on an older broker as rejected. `s.error` is the same
vocabulary the Mods-pane row shows: a rejected control adds a `{key, code,
message}` entry to that row's additive `warnings` array (`__test.statusOf(id)
.warnings`), and `code` there is byte-identical to `s.error` — what the
operator reads in the pane is exactly what the mod branched on. `state`
itself does not change; a mod with one degraded control is still `active`.
No widget mounts in the Control Panel for a rejected control — nothing to
click, nothing to reflect against a broken list. `s.error` is a decision
input, not a verdict: a fallback default is fine for a cosmetic control and
wrong for a load-bearing one, and only the mod knows which, so it reads the
code and chooses to degrade in place or bail out of its own `init()`
entirely.

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
   untouched.

**`validate` must be synchronous, permanently — this is contract, not a
temporary limitation (#203).** Two independent checks enforce it. At
*registration*, an `async function` is detected and the whole control is
routed down the degraded path above with `s.error === 'async_validator'` —
**fail-closed, not validator-stripped**: silently mounting the control with
its gate removed would just reintroduce the #168 bug (a `Promise` is truthy,
so a stripped-validator control would accept every write) one layer up,
invisibly. At *write time* the original #168 backstop still runs — a thenable
return, from a plain function that slipped past the registration check
(transpiled output and a bound function both read as an ordinary `Function`),
is rejected exactly as before, with `.set-err` showing an unhelpful "that
value could not be checked" rather than corrupting the write. Registration
detection is best-effort; the write-time check is the one that cannot be
fooled.

The reason this is not "await it, with a timeout" is structural, not a matter
of taste: `validate` runs on **every write attempt, and more than once per
commit**, and the commit pipeline that calls it is synchronous by contract —
the 400 ms debounce, the `change` flush, blur's flush-first ordering, and the
`pagehide` flush that catches a reload or tab-close mid-debounce cannot await
anything. `pagehide` in particular has nothing to await *into* — the page is
gone before a Promise would settle. And even where an await is technically
possible, an awaited verdict would judge a value that is stale by the time it
resolves: `entry.last` is set **before** the widget reflects (the #168
convergence-loop fix), specifically so a skipped or delayed reflect can never
mask non-convergence — a validator that suspends mid-pipeline reopens exactly
that gap.

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

### App windows: `ctx.windows.createAppWindow` (#194)

Nine shipped mods used to hand-build the same ~30-field app-window scaffold: a
`win` literal pushed straight into the core `windows` Map, a synthetic
`kind:'app'` session, a hand-appended taskbar chip, and a teardown that
iterated core state to find its own windows. Core now owns all of that; a mod
supplies only what is specific to *its* window. `mods/clipboard/clipboard.js`
is the worked example — it shipped as the reference migration alongside this
API, and its old scaffold, `CLIP_WIN_ID` hack included, is gone.

```js
const h = ctx.windows.createAppWindow({
    kind: 'clipboard',              // required: the appKind
    id: 'app:clip',                 // optional; see the singleton default below
    title: 'Clipboard',
    sid: 'clip', badge: '#clip',    // chip / session short id, taskbar badge
    appClass: 'app-clip', bodyClass: 'clip-body',
    singleton: true,                // open-or-focus, deduped on KIND — see below
    geom: undefined, color: undefined, locked: undefined, floatGeom: undefined,
    resizable: true,                // false = no resize handles (recorder's playback window)
    toolbar: function (el, win, h) { /* the .app-toolbar strip, optional */ },
    body: function (el, win, h) { /* the window's content */ },
    onClose: function () { /* replaces the × action; default is closeWindow */ },
});
```

`kind` is the only required field, and it must be a kind **this mod has already
registered** with `ctx.registerWindowKind` — register in `init()`, build later
(on a launch, a restore, a click). Two things ride on that: a mod cannot name
another mod's kind and have the dedupe hand it that mod's live window (which its
`closeAll()`, or simply its being disabled, would then close), and a window of a
kind nobody registered would persist a `/state` record nothing can ever restore.
A create is also refused outright while the mod is being torn down — the
take-down below walks one snapshot, so a window opened after it would outlive
both the pass and its kind. Both refusals throw.

Everything else falls back to the same
defaults the deleted scaffolds computed by hand: `title`/`sid` default to
`kind`, `badge` to `'#' + sid`, `appClass` to `'app-' + kind`, `bodyClass` to
`appClass + '-body'`, `geom` to `clampGeom(appDefaultGeom(kind))`, `color` to
`normalizeHex(defaultColor(id))`, `locked` to `true`, and `id` (absent
`singleton`) to a fresh `newAppId(kind)`.

Core owns, exactly as every deleted copy did it by hand: the chrome
(`buildAppChrome` + `wireAppChrome` + the eight resize handles), the desktop
insertion, the window record itself (the object that lands in the core
`windows` Map), the synthetic `kind:'app'` session and its taskbar chip
(`#taskbar-empty` removed once it exists), and the placement tail
(`finishWindowPlacement`). Your `toolbar`/`body` builders run *after* all of
that, against a window that already has a layout box and a record to hang
per-window state on — though not necessarily its *final* box, since
placement may still tile it.

`createAppWindow` returns a **frozen handle**, not the window record:

| member | does |
|---|---|
| `h.win` | the underlying window record — the escape hatch the trust model (§1) permits, not the API itself |
| `h.dom`, `h.body`, `h.toolbar` | the chrome root, the body element, the toolbar element (`null` if you passed no `toolbar`) |
| `h.isOpen()` | still a live member of the core `windows` Map |
| `h.focus()` | reveal-and-focus |
| `h.close()` | close this window |
| `h.setTitle(text)` / `h.setColor(hex)` | update title bar, session and taskbar chip together |
| `h.save()` | `saveAppWindow`, for a persisted kind |
| `h.addTitleBarItem(node)` | inserted before the minimize button |
| `h.onDispose(fn)` | `win.cleanups.push`, spelled the way `onTerminalCreate`'s bag spells it |

Every member is gated on the window still being live — a handle you keep
past its window's death (or past a *later* window reusing the same id) is
inert, not lethal.

**`singleton: true`** formalizes the open-or-focus hack every windowed mod
re-derived by hand (clipboard's old `CLIP_WIN_ID`). With it, `id` defaults to
the stable `'app:' + kind`, and a create call **dedupes on `kind`** over
every *live* window in the core `windows` Map — not just the windows this
mod's own registry already knows about, because core's unknown-kind restore
fallback (#167) may have rebuilt a window of this kind, under some other id,
before this mod ever loaded. Whichever window it finds is adopted (never
double-built) and, unless the call is a restore, brought to front. Without
`singleton`, dedupe is by `id` alone: a second create at a still-open id
adopts and focuses it; a create at an id mid-close throws (a reopen has to
wait for the close to finish); a create at an id already open under a
*different* `kind` throws rather than stranding the live window.

**`ctx.windows.list()`** — this mod's own live windows, oldest first, as a
fresh frozen array. Dead entries are pruned on the way past, so a `list()`
after a lease-loss rebuild reads empty instead of a wall of ghosts.
`mods/clipboard/clipboard.js`'s `renderAll()` is the shipped example: a loop
over `ctx.windows.list()` replaced a hand-kept `liveWins` set.

**`ctx.windows.closeAll()`** closes every one of this mod's factory-built
windows and returns how many it closed. It is the *same* pass the loader
also stages automatically: **a mod's factory windows are closed by core
before its `onUnload` chain runs, and while its window kinds are still
registered** — so a migrated mod needs no teardown-ordering care of its own.
That property is why `registerWindowKind`'s restore/teardown ordering used to
matter by hand: close a window *after* its kind is deregistered and the close
reaches `saveAppWindow` with no registry entry for the kind, falls back to
the shared serializer, and leaves a junk `/state` record for a kind nothing
can restore. Modeling the close as an `onUnload` entry would make it the
*oldest* entry in the LIFO chain and therefore run it *last* — after
`deleteWindowKind`, which rides that same chain — exactly the wrong order.
So it is never registered as an unload: the loader calls this same close
pass directly, at the head of teardown, before the first `onUnload` runs and
before any kind the mod registered is deregistered. This is what let
`clipboard.js` delete its own `ctx.onUnload` window-closing block entirely.
Call `closeAll()` yourself when you want a mod's windows gone without
disabling the mod.

**The restore hook's third argument.** `registerWindowKind`'s `restore` is
called *directly* by core, outside any mod's `ctx` closure, so it is handed
the per-mod window API as a third argument:

```js
restore: function (record, opts, api) {
    const h = api.createAppWindow({ kind: 'scratchpad', id: record.id, … });
    return h.win;        // or `return h;` — api unwraps a handle either way
}
```

`api` is `{createAppWindow, list}` — the same two members `ctx.windows`
exposes, scoped to this mod's record. Two defaults ride the call and both are
overridable in your own spec: `spec.id` defaults to `record.id` (so a restore
that builds elsewhere does not orphan the record it was handed, or step
around the id dedupe), and `spec.restoring` defaults to `opts.restoring`
(core's automatic restore pass always passes `true`) — the same suppression
`openAppWindow`'s own `restoring` gives: no reveal, no un-minimize, no
re-home to whatever workspace the page happened to boot on (#152).

**`ctx.windows.onAppWindowCreate(handler)`** fires for app windows whose
`appKind` is one of **this mod's registered kinds** — every kind it has
passed to `registerWindowKind`, however the window was actually built,
*including* a window core rebuilt through its unknown-kind fallback before
this mod ever registered that kind. It is **not** scoped to "windows this
mod's factory built." Subscribing **replays** every matching window that
already exists, then fires for every one built afterwards — exactly once per
subscription: a replayed window is never re-delivered as a live create, and a
live create is never re-delivered by a later replay, so a mod needs no
`WeakSet` of its own to avoid double-decorating. This is sticky's init-time
catch-up pass (`mods/sticky/sticky.js`) made first-class — whether a mod's
windows already exist when its `init` runs is a *race* (#167), not an
ordering, and this hook makes both orders come out identical.

```js
const off = ctx.windows.onAppWindowCreate(function (info) {
    // info: {win, id, kind, dom, body, titleBar, replayed, addTitleBarItem, onDispose}
    addChip(info.id);
    info.onDispose(function () { removeChip(info.id); });
});
```

`off()` also rides `ctx.onUnload` automatically, so a disabled mod stops
decorating windows even if it never calls the function it was handed.

**THE TRAP.** A `registerWindowKind` factory — `factory(appData)` — must
**`return h.win`, never the handle**: `openAppWindow` hands a factory's
return value straight back to its own callers (and its own id-dedupe branch
returns a window record too), so a factory that returns the handle breaks
every caller expecting a record. A **restore** hook is the one place a handle
*is* accepted — `api` unwraps it by identity before core ever sees it, so
`return h;` and `return h.win;` are both correct there, but a factory only
has the one right answer. `mods/clipboard/clipboard.js`'s
`openClipboardWindow` says exactly this at its own `return h.win` line.

### Core events: `ctx.events.on` (#195)

Every host- and state-lifecycle fact core wants to tell a mod has travelled as
an ad-hoc duck-typed protocol: a `win._onHostAuth` field core invokes **by
name**, a `win._hostRemoved` beside it, Help polling the core-private
`_stateReady` 20 × 500 ms for the initial adoption, workspaces reading the
core-private `_deactivated`. Nothing registers those and nothing types them, so
core cannot refactor its auth form, its host list or its boot sequencing without
auditing every mod. `ctx.events` is the one channel that replaces all of it —
and it is the channel to write **new** code against; migrating the remaining
readers off those private names is still in progress (#204).

```js
const off = ctx.events.on('state:adopted', function (payload, meta) {
    // payload — the table's shape below, frozen
    // meta    — {name, shape, gen, replayed}, frozen
});
off();      // unsubscribe; `off` also rides this mod's teardown automatically
```

**The table is the contract.**

| name | shape | payload | replay on subscribe |
|---|---|---|---|
| `host:auth` | edge | `{hostId}` | no |
| `host:changed` | edge | `{hostId}` | no |
| `host:removed` | edge | `{hostId}` | no |
| `state:adopted` | level | `{gen}` | **yes, exactly once** |
| `lease:changed` | level | `{active, gen}` | **yes, exactly once** |

A name this build does not declare is refused **inertly** — a `console.info`
and an unsubscribe fn that does nothing — never a throw, so a mod written
against a newer build keeps running here (§5's "absence is never an error").

**Edge vs. level is the whole design, not a flag.** An *edge* event is a thing
that **happened**: a subscriber that was not there missed it, and replaying it
later would be a lie about when it happened. A *level* event is a state that
**is**: a subscriber arriving late is entitled to the current one and gets it
immediately, exactly once, out of its own `on()` call. The two level events are
precisely the two a mod otherwise has to poll or read a core private for — the
replay is what a 20-tick `_stateReady` timer is *for*, done properly. A level
only replays once it *exists*: subscribe before core has learned the lease and
there is nothing retained to hand you, so you are told at the first transition
like everyone else.

**Exactly once — replay XOR live.** A subscriber that was there before the emit
is told live; one that arrives afterwards is told by its own replay. Never zero,
never twice — including the nasty case where a handler subscribes from inside
another handler, i.e. after an emit has been *applied* but before it has been
*delivered*. It is enforced per **subscription**, so two mods each get their own
exactly-once and a mod that unsubscribes and re-subscribes is told again.

**`gen` is stamped by the bus.** A `gen` key on the payload a core site passes
is **dropped**; the bus owns that field. It is a monotonic counter over every
emit that actually happened, across all five names — an ordering aid, so you can
tell a replay of `gen` 3 arriving after a live `gen` 5 is stale. It is **not**
the `/state` revision and **not** a lease epoch. Only a *level* payload carries
it; an edge does not, but the meta bag carries it for both. Payloads are a
shallow **copy** of what core passed, and frozen.

**A level emit that changes nothing does not happen.** Equality is shallow
`Object.is` over the own keys, ignoring `gen`: no generation is taken, no
handler is called, nothing is re-retained. That is why `state:adopted` fires
**exactly once ever** even though core has two adopt latches — the boot pull and
the lease-loss rebuild both emit an empty payload, so whichever runs second is a
no-op — and why a repeated `{active: false}` from the lease socket is silent.

**Fire-after-apply.** Core emits *after* it has mutated, and the bus applies its
own state (the generation, the retained level) before it calls anybody, so a
handler observes finished state rather than a half-applied one. Two limits the
code states out loud and this page will not overstate:

- `lease:changed` is **HOME only**. The payload has no `hostId` — one retained
  level cannot speak for N brokers, and a remote broker's lease is a per-host
  mask with no event in this build's table.
- On a lease **loss** the view is fully torn down before any handler runs; on a
  **gain** the rebuild is still in flight. The event names the **lease**, which
  core has genuinely learned, not the view.

**Handler isolation.** A throwing handler is logged and its siblings still run,
and the throw never reaches the core mutation site that emitted.

**Auto-unsubscribe at teardown.** `off` is pushed onto this mod's LIFO teardown
chain as well as returned, so a disabled mod stops hearing about hosts even if
it never calls the function it was handed.

**Re-entrancy is coalesced and bounded.** An emit raised from inside a handler
joins the pass already running instead of nesting one, so a cascade's stack
depth is constant and the delivery order stays the order things were applied in.
Level events coalesce while still queued (two lease changes inside one pass
deliver the final state once); an **edge never coalesces**, because two auth
successes are two facts. The pass is capped — exhausting it drops what is left
with a `console.warn`, rather than livelocking the UI thread.

**Only core emits.** `ctx.events` carries `on` and nothing else. A mod that
could emit `host:removed` could lie to every other mod about core state, and
inter-mod messaging is a separate surface with its own trust story (#199).
Adding an emit later is additive.

**Where each event comes from**, so you know what it means:

- `host:auth` — a completed host authentication, and a poll that finds a host's
  auth has recovered.
- `host:changed` — a committed host add/edit, the host colour swatch, and the
  hidden toggle. Only paths that actually mutated emit it. The edge carries an
  id, not a snapshot: re-read what you care about and decide.
- `host:removed` — *after* the whole removal has finished, so it means "that
  host is gone", not "it is going".
- `state:adopted` — the first `/state` adoption, whether that is the boot pull
  or a lease-loss rebuild that beat it.
- `lease:changed` — the home single-active lease, per control frame.

**Feature-detect it.** `events` is its own capability family at version 1, so
`ctx.capabilities.events`, `if (ctx.events)` / `typeof ctx.events.on ===
'function'` and `needs: ['events']` all work. `mods/help/help.js` is the shipped
worked example — its corpus refetch on `host:auth`, replacing the
`win._onHostAuth` field it used to write. Note it **feature-detects instead of
declaring `needs: ['events']`**, deliberately: a `needs` blocks the whole mod,
and Help without the bus is missing one corpus refresh, while Help blocked has
no chip, no window kind and no hotkey. Weigh that trade the same way.

Core still invokes `win._onHostAuth` by name for the three mods #204 has yet to
migrate. That is legacy, not a pattern — do not add a fourth.

### Per-window teardown: `info.onModTeardown` (#195)

A mod that decorates a **terminal** window has two exits to cover, owned by two
channels, neither of which covers the other:

| chain | fires when | does **not** fire when |
|---|---|---|
| `win.cleanups` — `info.onDispose` | the **window** closes (`closeWindow`, and the lease-loss rebuild) | the mod is disabled with that window still open |
| `rec.unloads` — `ctx.onUnload` | the **mod** is disabled | one of the windows it decorated closes |

So a widget needed a disposer on both, an idempotence flag so the first exit
disarmed the second, and a set so the mod-side entry could still reach the open
windows. Core keeps all of that now:

```js
ctx.windows.onTerminalCreate(function (info) {
    info.addTitleBarItem(btn);
    if (!(typeof info.onModTeardown === 'function'
          && info.onModTeardown(teardown))) {
        info.onDispose(teardown);        // refusal / older build — see below
    }
});
```

- **Exactly once, on the first of the two.** Both exits fire the same
  per-(mod, window) state, and it is marked spent before its callbacks run — so
  close-then-disable, disable-then-close, and a callback that closes its own
  window or disables its own mod from inside the teardown it is running in are
  all the same single call.
- **A disable does not close the window.** A terminal belongs to core, not to
  the mod that decorated it: this removes the decoration and leaves everything
  else where it was. (#194's staged take-down *does* close a mod's windows at
  disable — those are the mod's own factory windows. A terminal never is.)
- **It returns a boolean**, and a refusal is never a throw: `false` for a
  non-function, for a window that is already gone, for this mod being
  mid-teardown, and for a state that has already fired. You can see the `false`
  and run your own cleanup, which is exactly what the fallback above is.
- **Callbacks run LIFO**, each isolated, so one broken disposer strands neither
  the window's remaining ones nor the chain behind them.
- **The state is per (mod, window), not per delivery.** A mod may hold more than
  one `onTerminalCreate` subscription and every one is handed a bag for the same
  window; they all register into one state, so a window carries one
  `win.cleanups` entry per **mod** however many teardowns that mod registers on
  it.
- **Where it sits in the LIFO chain.** The mod-side entry is pushed at your
  first accepted registration — normally inside `init()`, on the
  `onTerminalCreate` replay — so it is an *early* entry and therefore runs
  *late*, after disposers you registered afterwards. A self-contained widget
  teardown belongs there; if you need your widget gone *before* some other
  resource you own, tear that one down from the same callback.
- **No new capability entry, and `needs` cannot name it.** `windows` is already
  a v1 family and this adds a member to it, so the map is unchanged; `needs`
  resolves a path on **`ctx`** (§5) and the per-delivery info bag is not
  reachable from `ctx`. Feature-detect with `typeof info.onModTeardown ===
  'function'`.
- One accepted cost: a disable leaves its spent `win.cleanups` entry on the
  window. Splicing it out at fire time is the one thing this must not do —
  `closeWindow` iterates that array with `for…of`, and removing an entry
  mid-drain skips the next one — so an enable/disable cycle on a long-lived
  terminal leaves one dead closure per cycle.

**`mods/git/git.js` is the shipped reference migration.** Its hand-kept
`disposers` Set, the `ctx.onUnload` block that drained it and the decorate-once
`decorated` `WeakSet` in front of both are all gone, replaced by the one
registration above at the same site. (The `WeakSet` needed no replacement:
`onTerminalCreate` already delivers each window once per subscription.) What it
**keeps** is the `info.onDispose` fallback, deliberately: `onModTeardown` is
absent on a page assembled without the extension fragment and returns `false`
on a dead window or a mod mid-teardown, and in those cases arming the
window-close half alone is still worth it — a leaked 15 s poll plus a live
document-level listener on every terminal is the worse failure. It is the one
line the mod rode before, not a second set.

### Forgetting a host: `ctx.hosts.invalidate` (#195)

`hosts` is a **new family** at capability version 1, and `invalidate(id)` is its
first member: "forget everything this page knows about host `<id>`".

It clears **every registered per-host cache** and then repaints the host
surfaces (the hosts list, the settings tabs, the host status chips and the
taskbar). The list is core's, not a caller's — each cache registers itself
beside its own declaration — so the eight this build has (the per-host state
cache and its save chain, the auth-prompt latch, the poll record, the `/control`
socket, and three config caches) *and* whatever the next fragment adds are
covered by the one call. The cautionary example is in the tree:
`mods/host-registry/` still carries its own hand-maintained copy of that list,
and its own comment calls that copy "a known latent gap". A list kept by a
stranger is right on the day it is written and nothing makes it stay right —
call this instead.

It returns `true` when it ran and `false` for a blank / non-string id (nothing
to forget, and no repaint owed) or an internal failure — **never a throw**, so
calling it from inside an event handler cannot take a repaint down with a bad
argument.

Two things it deliberately does **not** do:

- **It does not emit.** Invalidation and notification are separate acts: only
  core *mutation* sites emit `host:changed`. So a `host:changed` subscriber may
  call `ctx.hosts.invalidate()` without recursing.
- **It does not save.** `savePrefs()` is a mutation (localStorage plus a
  `/state` push) and stays with the caller that actually changed something, so a
  mod can drop stale per-host state without writing prefs it does not own.

Clearing a cache does not cancel an in-flight request; it drops the stale value
so the *next* request uses the new url/token, and the poll loop re-primes on its
next tick.

Feature-detect with `typeof ctx.hosts.invalidate === 'function'` or
`needs: ['hosts.invalidate']`. On a build whose core has no invalidation path
the family is not installed **at all**, so `ctx.capabilities` reports `hosts`
absent — true — rather than handing you a member that silently does nothing.

### Compare-and-swap writes: `ctx.serverStore.update` (#196)

```js
const res = await ctx.serverStore.update(fn, {
    host?, retries?, purgeRevisions?, noHistory? });
// -> {ok:true, rev, value} | {ok:false, error, status?, rev?, value?}
```

`get` → `fn(value, rev)` → `set`, and **on a `409 conflict` it re-reads and
re-applies `fn` to the winner's value**. The blob that lost is never re-`PUT`.
That single behaviour is the whole reason the helper exists.

**`fn` must be pure over `(value, rev)`.** It is invoked **once per attempt**,
so a side effect inside it happens once per attempt — bumping a counter,
appending to a log, showing a toast, queuing another save. That is a caller
bug, and the code deliberately does not defend against it, because the only
available defence is to snapshot once and re-send that snapshot after a `409`
— which *is* the #158 clobber, now with retries. Everything the write depends
on that can change between attempts (the live editor buffer, the current
selection, `Date.now()`) belongs **inside** `fn`, where it re-runs over the
winner.

Which is also why the shape is a **reducer and not a snapshot-getter**. An
`update(gatherValue)` API cannot rebase: it has nothing to apply the winner's
value to, so its only move after a `409` is to overwrite. The winner's value has
to flow *through* your function.

The rest of the contract:

- **`value` is the winner's value**, freshly parsed on every attempt, and
  `null` on a store nobody has written yet. `rev` is the revision it was read
  at. Mutating `value` in place is safe, but the next value must be
  **returned** — a reducer that only mutates returns `undefined`, which is
  refused (`bad_value`) **before** any write, because the server 400s on a
  missing `value` and a round trip is a slow way to learn that.
- **An async reducer is first-class** — `fn` may return a promise.
- **Bounded, and the conflict is surfaced.** One write plus a bounded number of
  rebases; `retries` moves that budget and is **clamped** — the ceiling is not
  the caller's to remove, because the mirror failure of a silent clobber is a
  silent infinite rebase against a hot writer, and every attempt also writes
  into the store's revision ring (#175). Exhausting the budget resolves
  `{ok:false, error:'conflict'}` with the last winner's `rev` and `value` still
  inlined, and **you** decide whether to widen, warn or drop.
- **`not_active` is refused, not rebased.** It is a `409` too, but it means
  this browser does not hold the `/state` write lease, so re-applying `fn` at a
  fresher revision would burn the entire budget only to be refused again. It
  comes straight back with the live `rev` inlined.
- **A `409` that inlines the live value costs no extra round trip**; one that
  does not (an older broker, or a remote one whose body is untrusted input)
  falls back to an authoritative re-read — and **a failed read aborts without
  writing**. So does the *initial* read: a response that is not this store's own
  `200` shape is a refusal, never "an empty store", because reading it as
  rev 0 / `value: null` would run your reducer down its "nothing stored yet"
  branch on a store nobody could actually read.
- **`opts` is snapshotted once and rides every attempt**, rebases included, so
  `purgeRevisions` (#65) and `noHistory` (#192) cannot be dropped by a retry.
  `noHistory` keeps `set()`'s strict tri-state: it is **presence-keyed**, and
  **absent is not `false`** — absence means "leave this record's flag exactly as
  it is", so a retry that helpfully sent `noHistory:false` would resume
  archiving the very credentials the flag exists to keep out of the ring.
- **Fail-closed, inherited.** Every request goes through your own
  `ctx.serverStore.get`/`set`, so it is mod-scoped, lease-carrying and
  host-routed exactly as they are: an unknown `opts.host` makes **no request**
  and refuses before any `PUT` is attempted.
- **It never rejects.** Every outcome resolves to a result, including
  `{ok:false, error:'fn_failed'}` when your reducer throws (it is logged) and
  `bad_fn` when the first argument is not a function.

Feature-detect with `typeof ctx.serverStore.update === 'function'` or
`needs: ['serverStore.update']`. It is a new **member**, not a new family, so
there is no capability entry for it: `ctx.capabilities` is keyed by family name
(`serverStore`), never by dotted path — the same as
`ctx.windows.createAppWindow`. `ctxVersion` stays `1`.

### The save chain: `ctx.serverStore.saveChain` (#196)

```js
const chain = ctx.serverStore.saveChain({
    host?, debounceMs?, retries?, purgeRevisions?, noHistory? });
chain.save(fn);      // -> true (queued) | false (a dead chain, or a non-fn)
chain.flush();       // -> Promise<result>, never rejects
chain.onState(fn);   // 'idle' | 'saving' | 'conflict'  -> unsubscribe fn
```

`scratchpad`'s save pipeline, canned. It adds **nothing** to the write itself —
every send goes through `update()`, so the `409` rebase, the bounded budget, the
fail-closed host lookup and the verbatim `purgeRevisions` / `noHistory`
passthrough are inherited rather than re-implemented. What the chain owns is
*when* to write, *how many* writes to make, and *what to tell the user* while it
happens.

`save(fn)` takes the **same pure `(value, rev)` reducer** `update()` takes, and
the purity rule above applies unchanged — including this specific case: **do not
call this chain's own `save()` from inside a reducer**, or a rebase queues the
same work twice.

- **Coalesced by composition, not by replacement.** Rapid saves queue, and the
  queue is spliced into **one** batch composed in order — `fn2(fn1(value, rev),
  rev)` — handed to `update()` as a single reducer. So two queued saves **both
  apply**; an earlier caller's delta is never dropped on the floor. For a
  reducer that produces the whole document every time (scratchpad's) that
  degenerates to last-wins, which is exactly the old behaviour. Two
  consequences: every reducer in a batch sees the **same** base `rev`, and one
  reducer that throws or forgets to return refuses the **whole** batch, under
  `update()`'s own error names.
- **Single in flight, per chain.** At most one write from a given chain is
  outstanding; saves arriving during it leave as exactly one more write. *Per
  chain* is the honest scope — two chains in one mod address the **same**
  per-mod record on a given broker (`opts.host` only picks whose copy), so they
  serialize with each other only through the server's revision CAS, each
  re-applying over the other's winner. Correct, but it spends conflict budget:
  write one chain per record.
- **Trailing debounce.** Each `save()` pushes the deadline out, which is what
  makes a burst one write — and means a genuinely continuous writer only leaves
  via the in-flight and `flush()` paths. `debounceMs` overrides the default and
  is **clamped**, for the reason in the next bullet: a pending batch is dropped
  at teardown, so an absurd debounce is data loss wearing a save's clothes.
- **`onState`** reports `'saving'` while a write is outstanding, `'conflict'`
  for an unresolved refusable `409` (`conflict` or `not_active` — scratchpad's
  read-only banner), `'idle'` otherwise. It is deduplicated, it **fires once on
  subscribe** so a banner mounted after the fact is correct without
  reconstructing anything, a throwing listener cannot cost the chain its pump,
  and the returned unsubscribe is yours to call.
- **`flush()`** forces the pending batch past the debounce and resolves once an
  in-flight write and anything already queued have completed — never waiting on
  writes queued *after* it. With nothing outstanding it resolves
  `{ok:true, idle:true}`, except while a conflict is unresolved, where it
  reports that result rather than a fresh `ok`.
- **It never retries a failed batch by itself.** `update()` already rebases a
  `409` up to its budget; past that the batch is dropped and the chain reports
  `'conflict'`. Scratchpad's answer is to re-attempt on the next interaction
  while the banner is up; yours is yours.

**The trap: a chain drops its pending batch at teardown.** Deliberately. The
timer is cleared, the queue is discarded, the state listeners are released, and
every outstanding `flush()` promise resolves `{ok:false, error:'unloaded'}`
rather than hanging the caller awaiting it. From the first instant of a disable
the chain reads dead — `save()` returns `false`, `flush()` resolves
`{ok:false, error:'unloaded'}`, and a disable landing *between* `update()`'s read
and its write is a drop, not a race.

That is not the same posture as the `ctx.settings.text` debounce, which *does*
flush on teardown, and the difference is the point: a settings commit reaches
`localStorage` synchronously, so a teardown can complete it, while a chain write
is a network round trip that a **synchronous** disable cannot await. "Flush on
the way down" is precisely the write that lands *after* a re-enable and clobbers
the new activation.

So **a deliberate final flush must be called while the mod is still alive** —
from a window-close cleanup (`win.cleanups`, which also runs on an ordinary
close, with the mod up) or from an explicit save action such as a `Ctrl+S`
handler. **Never from `ctx.onUnload`**, where the activation is already marked
dead and the batch is dropped by design. `mods/scratchpad/` is the worked
example on both counts: it flushes from `win.cleanups` (capturing the editor
buffer *first*, because a later cleanup in the same drain destroys the editor
view while `flush()` is still resolving), and its `Ctrl+S` path is a `save()`
followed by a `flush()`. What no client-side check can recall is a request
already handed to the network; that residual window is named here rather than
papered over.

Feature-detect with `typeof ctx.serverStore.saveChain === 'function'` or
`needs: ['serverStore.saveChain']` — a **member**, so no capability entry, same
as `update()` above. Scratchpad declares the `needs` and blocks: saving *is*
that mod, so a build without the chain should read
`blocked (needs serverStore.saveChain)` rather than open a notes window that
silently drops every keystroke. `mods/help/` makes the opposite call for
`ctx.events` and feature-detects instead, because a missing bus costs it one
corpus refresh rather than all of its persistence. **Block when the loss is
total; degrade when it is partial** — the two shipped mods are there to be
copied from, in whichever direction fits.

### Browser-local preferences: `ctx.prefs` (#196)

```js
ctx.prefs.get(key, default);   // -> the stored JSON value, or `default`
ctx.prefs.set(key, value);     // -> true once it reads back
ctx.prefs.remove(key);         // -> true (false only for a bad key)
```

A mod's own preferences, per **browser**, with a contract instead of a
coincidence. Before this, both ways of reaching for core's `prefs` object were
wrong: a **bare** key is wiped by "Reset local view"'s non-underscore sweep, and
an **underscore** key stays local only because the `/state` blob happens to
serialize `_settings` and `_layout` and nothing else — one edit there and it
syncs, exactly as `_settings` already does (#153).

**The namespace is the decision.** `webterm:modprefs:<id>:<key>`, one
`localStorage` record per key, a **sibling** of the `webterm:mod:<id>:`
namespace `ctx.storage` owns — never a sub-prefix inside it, and never a key on
the core `prefs` object. Three consequences, each of them the point:

- **"Reset local view" cannot reach it.** That sweep deletes keys off the
  `prefs` *object* and re-serializes it; a `modprefs` record is not in it.
- **The sync path cannot reach it.** The `/state` blob is built from
  `prefs._settings` and `prefs._layout`, so a `ctx.prefs` write leaves the
  serialized state byte-identical and no `/state` `PUT` is attributable to one.
  It is not "local by convention" — it is structurally unable to sync. A future
  "sync my prefs" is therefore a **new tier**, not a flag on this one.
- **`ctx.storage` keeps its semantics exactly** — no reserved-prefix policing,
  and no way to clobber a preference record through the raw surface, since a mod
  id may not contain `:` and both namespaces end in one.

Values are **JSON**, not strings (that is the other difference from
`ctx.storage`). The failure posture is `ctx.storage`'s, copied deliberately:

- `get(key, default)` returns `default` for a missing key, a bad key and a
  **corrupt record** — a half-written or hand-edited value is a miss, never a
  throw that takes down the `init()` reading it.
- `set` returns `false` for a bad key or a value JSON cannot represent (a cycle,
  `undefined`, a function) — and in that case **nothing is written**, so a
  refused `set` never destroys what is already stored.
- A `localStorage` that refuses (private mode, quota) is swallowed and the value
  is kept **in memory for the life of the page** — not the activation — so a
  disable and re-enable still finds what `localStorage` would have kept. A
  refused `remove` reads as removed for the rest of the session rather than
  resurrecting the record it could not delete.

`prefs` is a **new family** at capability version 1, so it does have a
capability entry: gate with `ctx.prefs && typeof ctx.prefs.get === 'function'`,
`ctx.capabilities.prefs`, or `needs: ['prefs']`. `ctxVersion` stays `1`.

And it is storage, not a boundary: like everything else on this page (§1), the
records sit in the page's own `localStorage`, where any script on the origin can
read them. Namespacing keeps mods from *colliding*, not from *looking*.

### Per-activation cancellation: `ctx.signal` (#198)

```js
ctx.signal                             // an AbortSignal for THIS activation

const res = await ctx.file.read(path, { signal: ctx.signal });
if (res.aborted) return;               // this resumes AFTER teardown finished —
statusNode.textContent = res.error;    // check before touching anything of yours

if (!ctx.signal.aborted) {             // `.aborted`, never bare `if (ctx.signal)`
    const t = setTimeout(poll, 5000);
    ctx.signal.addEventListener('abort', function () { clearTimeout(t); });
}
```

One `AbortSignal` per **activation**, aborted when that activation ends. It
replaces the dead-flag pattern mods hand-rolled to stop an in-flight loop from
touching a desktop it no longer belongs to (`mods/update/`'s `restartOpDead` /
`applyOpDead` are the worked examples, and are still written by hand — the
migrations are not part of this).

- **Per activation, and freshness is structural.** The controller hangs off the
  per-mod record `initMod` builds, and it builds a new one every time a mod
  comes up. A disabled-then-re-enabled mod therefore gets a **new, un-aborted**
  signal; the old activation's signal stays aborted forever, which is the point
  — its loops must not come back to life when the mod does.
- **The abort is the first thing teardown does.** In order: the record is marked
  as unloading, **then the signal aborts**, then #194's staged close of the
  mod's factory app windows, then the LIFO `onUnload` chain (which is where
  #195's per-window `onModTeardown` disposers ride, as an entry on that list).
  Abort **listeners** fire synchronously, so a listener is told to stop *before*
  anything starts taking your windows away, and before every `onUnload`. That
  ordering belongs to listeners and to nothing else — read the next bullet
  before you assume it covers an `await`.
- **Cancelling the work is not cancelling the continuation.** The take-down
  path is entirely synchronous: by the time an aborted `await` resumes — a later
  microtask — the staged window close and the whole `onUnload` drain have
  already run to completion. So the line *after* your `await` executes against a
  torn-down activation, and for `ctx.file` that is worse rather than better,
  because an abort **resolves** `{ok:false, aborted:true, error:'cancelled'}`
  instead of throwing: there is no rejection to fall out of, and the naive
  `statusNode.textContent = res.error` writes into a window that is gone. **A
  continuation that touches activation-owned state must check first** — the
  result's `aborted`, or `ctx.signal.aborted` — which is precisely the job
  `mods/update/`'s `restartOpDead` / `applyOpDead` still do by hand. The signal
  cancels the request for you; it does not return you to a live mod.
- **`if (ctx.signal)` is a capability check, not a liveness check.** The ctx is
  not dismantled at teardown, so `ctx.signal` stays truthy forever — it is the
  *aborted* signal by then. An async callback that resumes after teardown, sees
  a truthy `ctx.signal`, arms a `setTimeout` and registers an abort listener to
  clear it has built a leak: an already-aborted signal never replays its abort
  event to a listener added afterwards, so that listener never runs and the
  timer fires against torn-down state. (Core codes to the same fact — `hostFetch`
  tests `callerSignal.aborted` up front rather than relying on a listener.)
  Check `ctx.signal.aborted` **before arming anything** on a path that can
  resume late.
- **An abort listener is teardown-time mod code.** It runs after the record
  reads dead, so from inside one a `saveChain` is already dropping batches and a
  settings write cannot re-enter your own theme subscriber. It is not a place to
  rescue a last save from — see the pairing rule below.
- **Explicit pass, never ambient.** The loader injects `ctx.signal` into
  nothing. `ctx.file` and `ctx.session` forward `opts.signal` only when *you*
  hand it over, and `hostFetch` **composes** it with that call's deadline
  instead of replacing it, so a cancel and a timeout stay distinguishable at the
  result. An ambient signal would silently change every existing call site and
  abort work a mod deliberately wants to finish.
- **An aborted call still resolves; it never rejects.** `ctx.file` maps the
  abort to `{ok:false, aborted:true, error:'cancelled'}` — `'cancelled'`, the
  word the file family already used for a user pressing Cancel, distinct from
  its `{ok:false, timedOut:true, …}` deadline. `ctx.session` keeps its
  status-carrying envelope and resolves `{status:0, json:{ok:false,
  error:'AbortError: mod "<id>" was torn down'}}` — the stringified reason,
  named so a console says *which* mod's teardown cancelled the request.
- **Feature-detect it as a value, not a function.** `if (ctx.signal)`,
  `ctx.capabilities.signal`, or `needs: ['signal']`; `typeof ctx.signal ===
  'function'` is the detection that would be wrong, because unlike every family
  whose members are callable this one *is* the value — which is also why it owns
  a capability entry rather than being gated as a member of one. A build whose
  engine has no constructible `AbortController` gets **no `ctx.signal` at all**
  rather than a placeholder that never aborts, and `ctx.capabilities` reports it
  absent, so `needs: ['signal']` blocks with a reason instead of running against
  a signal that quietly does nothing. `ctxVersion` stays `1`.

**The pairing rule: work that is *supposed* to outlive the teardown must not
carry the signal.** They are two halves of one decision, and reading only the
first half is how a migration breaks a save. The shipped referent is #196's
deliberate final `flush()`: `mods/scratchpad/` calls it from a **window-close
cleanup** precisely so a last edit reaches the server, and neither
`serverStore.update()` nor `saveChain` takes a `signal` at all — there is no
option to pass one, and that is deliberate. The failure to avoid is threading
`ctx.signal` through the writes *you* make on that same path (a final
`ctx.file.write`, a last `ctx.session` call): a disable would then abort exactly
the work you added the cleanup for.

The rule is about **the work's lifetime, not the callback it starts in**, and
this is worth being exact about, because the clearest teardown-surviving write
in the repo starts from an `onUnload`. `mods/recorder/` stops a live recording
from its unload disposer and saves what it captured — and that disposer runs
*after* the abort, by construction. It is safe today only because nothing hands
it a signal; give it one and `hostFetch` short-circuits an already-aborted
caller signal before it ever calls `fetch`, so every disable with a live
recording would lose the segment. So do not read this as "close-time paths are
the risky ones": **if it must survive the teardown, it does not take the signal,
wherever it is started from.**

---

### Inter-mod services: `ctx.provide` / `ctx.consume` (#199)

```js
// the PROVIDER, from its init:
if (typeof ctx.provide === 'function') {
    ctx.provide('pattern', { apply: applyPattern });
}

// the CONSUMER, PER USE — not once at init:
function apply(name) {
    applyTheme(name);
    try {
        const p = (typeof ctx.consume === 'function')
            ? ctx.consume('pattern', 'pattern') : null;
        if (p) p.apply(getSettings().pattern);   // absent/inactive -> undefined
    } catch (_) {}
}
```

One mod publishes a named api; another asks for it by **provider id and name**.
It replaces the cross-mod call this repo actually had — `mods/pattern/` left
`applyPattern` at the top level of the one shared `<script>` and `mods/theme/`
probed `typeof applyPattern === 'function'` before calling it. That probe
answers exactly one question ("did some fragment declaring that name
evaluate?") and a top-level `function` outlives the mod that declared it, so it
reads *true* for a mod switched off ten minutes ago and the call lands in a
torn-down closure. The pattern/theme pair above is the shipped reference
migration; the remaining consumers are not migrated yet.

- **`consume` implies no dependency edge.** It returns `undefined` for a
  provider that is absent, not installed, installed but not loaded this page,
  disabled, mid-teardown, or that never published that name — never an error.
  That soft miss is the design, not a gap: an implicit edge would turn a soft
  probe into a take-down cascade, so disabling `pattern` would take `theme` down
  with it. **`requires` stays the only way to force ordering and bring-up.**
  Corollary: if you need the service **at your own `init()`**, declare
  `requires`; otherwise **consume lazily, per use**, which is what the theme
  example does and why nothing there has to be revalidated — every call asks the
  loader's active-mod map afresh.
- **What you get back is a revocable proxy, one per provider activation.**
  After the provider tears down every read returns `undefined`, every call
  no-ops, and there is **exactly one `console.warn`** naming the service, ever
  (the flag lives on the entry, so a dead proxy hammered in a render loop warns
  once). A function-shaped member read off a dead proxy returns a **no-op
  function** rather than `undefined`, so `p.load()` on a dead service does not
  throw *"p.load is not a function"* inside your code; which keys are
  function-shaped is snapshotted at `provide` time, string keys only. A function
  you plucked while it was live (`const apply = p.apply;`) re-checks liveness at
  **call** time and no-ops too.
- **Deadness starts at the head of teardown**, not at some disposer's turn in
  the LIFO drain: the record is marked `unloading` first, and every trap reads
  that. A synchronous frame already inside the provider still runs to completion
  — no proxy can unwind a stack — but every further operation through every
  proxy for that activation is dead, including one issued from the in-flight
  call's own continuation.
- **The honest limit: this is teardown-race hygiene, not a security boundary.**
  Values a consumer already **extracted through** the api are not revoked. A
  function read off the proxy is (see above), but a **nested object**
  (`p.config`), a value **returned** by a member, and anything obtained by any
  other route are the provider's own objects and keep working. Deepening the
  proxy would not change that — a service can always hand out a live closure —
  and a mod that can call another mod's api can already do whatever that api
  does. Per-use `consume` is the recommended pattern precisely because it needs
  none of this.
- **A throwing member is isolated**: logged, and you see `undefined`. So a
  consumer cannot distinguish "member threw" from "provider dead" from "no such
  member" — all three are `undefined`, which is what a soft seam costs. Only a
  *synchronous* throw is caught; a rejected promise a member returns is yours to
  handle.
- **`provide` is the strict half, and the only one that throws:** a non-empty
  string name and an object-or-function api are required, and re-providing a
  name this activation already has live raises the loader's `ModConflictError` —
  two halves of one mod claiming one name is a programming error, unlike
  consume's miss. A throw from `init()` is already handled (the mod is rolled
  back). Calling `provide` from an async continuation that resolved **after**
  the mod was switched off is *not* an error: it publishes nothing, quietly.
- **Two mods providing the same name is a non-event**, and a mod consuming
  itself, or a name it also provides, just works: there is no global service
  table to collide in — entries live on each activation's own record and
  `consume` names the provider.
- **Feature-detect, and expect both or neither.** They install as a pair:
  `if (ctx.provide)` / `ctx.capabilities.provide` / `needs: ['provide']`, same
  for `consume`. An engine without `Proxy`/`Reflect`/`WeakMap` gets **neither**
  member — not a `consume` that would have to hand back the raw api and silently
  drop revocation — and `ctx.capabilities` reports both absent, so
  `needs: ['consume']` blocks with a reason. Note that a `needs` declaration
  **blocks** the mod on a build that lacks the seam, which is why
  `mods/theme/mod.json` deliberately does *not* declare one: it degrades to "no
  pattern" instead. `ctxVersion` stays `1`.
- **Keep the `typeof` guard even with the seam.** The theme example's
  `try/catch` is not the old `ReferenceError` guard: `ctx.consume` is *absent*
  on a build without the seam, where calling it is a `TypeError`, and the code
  around the call is your own. The proxy isolates only throws from **inside**
  the provider's member.

---

### Named dispatch targets: `ctx.commands` (#199)

```js
const off = ctx.commands.register('save', {
    scope: 'window',                  // 'global' (default) | 'window'
    when: function (where) { return where.win.kind === 'note'; },
    run: function (args, where) { return save(where.win); },
});                                   // -> registered as 'scratchpad:save'

const r = await ctx.commands.execute('scratchpad:save', args);
// {ok:true, value} | {ok:false, reason:'absent'|'inactive'|'blocked'|'error'}
```

A **named, owned** dispatch target, replacing the duck-typed fields core pokes
by name — scratchpad's Ctrl+S rides `win._saveToServer`, and help hoists
functions to the top level on purpose because the mod system could not
contribute a keybinding target at all. Both fail the same way: a typo'd id, a
disabled mod's `undefined` field and a real failure are all "nothing happened".
(Those call sites are not migrated by this; the surface is new.)

**The outcome vocabulary is the point, and it is closed and branchable:**

| `reason` | means |
| --- | --- |
| `absent` | nothing ever registered that id **in this page** — a typo, a mod that is not *installed*, or one that has never been switched on this session (a disabled mod never ran `register`, so its ids are `absent` rather than `inactive`, and enabling it does fix them). |
| `inactive` | the id was registered by a mod that is **not active now** — switched off, mid-teardown, or torn down since. Fixable by toggling that mod on. |
| `blocked` | the command **declined this invocation**: `when()` said no, or it is window-scoped and no window is focused. |
| `error` | the command **failed to answer** — `run` threw, its promise rejected, or `when()` threw. |

`execute` **always returns a Promise and that promise never rejects**, because
its callers are a key dispatcher and a menu click handler: one bad command must
not take either down. Branch on `reason`; a `blocked`/`error` result also
carries a `detail` (`'no-window'` / `'when'`) or the `error` object, both
informational only — `reason` is the part that is closed and stable.

- **`register` takes a LOCAL id and auto-prefixes `'<modId>:'`; `execute` takes
  the FULL id.** A local id containing `':'` is **refused with a throw**, which
  is what makes a cross-mod id unrepresentable rather than merely discouraged:
  mod `b` cannot spell `'a:save'`, and a mod helpfully passing its own
  `'a:save'` gets a throw at init instead of a live `a:a:save` nothing
  dispatches. A duplicate within one mod is a `ModConflictError`. `register`
  returns an unregister function, and also rides the activation's unload chain,
  so teardown releases it whether you call it or not.
- **A window-scoped command with no focused window is `blocked`, and neither
  `run` NOR `when` is called.** It does not run with `win: null`. Handing every
  window-scoped command a null it must re-check would rebuild, inside every mod,
  exactly the `if (typeof win._saveToServer === 'function')` guard this exists
  to delete. `scope` is a **precondition**, and an unmet precondition is
  `blocked`.
- **A `global` command gets `{win: null}` even when a window is focused.**
  `scope` is a contract, not a hint: passing the focused window
  opportunistically would make a global command quietly window-sensitive, and
  would make a later `global` → `window` change a no-op that alters nothing
  until the day nothing is focused. A command that wants the focused window says
  so.
- **A throwing `when()` is `error`, not `blocked`, and `run` is not called.** A
  guard that threw did not say no — it failed to answer, and mapping that onto a
  deliberate veto hides a broken guard as a normal "not right now" forever.
  `blocked` is a decision; `error` is a defect.
- **An async `when()` is resolved, not coerced.** `!!promise` is `true`, so the
  coerced version would let every async guard allow **everything** (shipped once
  already, as #168's async-validator bug). It costs the command its synchronous
  dispatch, though — see the next bullet — and after the guard resolves the
  activation is re-checked, so a mod torn down while its own guard was pending
  answers `inactive`.
- **`run` is called synchronously**, in the caller's tick; only the *result* is
  wrapped in a Promise. A command dispatched from a click or a keydown may need
  the user gesture (clipboard writes, fullscreen, `window.open`), so a
  gesture-gated command wants a **synchronous** `when`.
- **A disabled mod's command answers `inactive`, not `absent`.** Teardown does
  not delete the entry; it replaces it with a **tombstone** keeping the id and
  the owner and dropping the closures. Deleting would collapse "turn that mod
  on" into "that id does not exist". Mid-teardown counts: a command executed
  from an abort listener or another disposer already reads `inactive`.
- **The menu path never calls `when()` at render time** to grey an item out. Two
  arbiters — a synchronous render-time guard and an asynchronous dispatch-time
  one — is how a menu shows an item enabled and then does nothing. `execute` is
  the single arbiter, at click time, so no mod code runs during a menu render.
- **Feature-detect it:** `if (ctx.commands)`, `ctx.capabilities.commands`,
  `needs: ['commands']` (or the finer `needs: ['commands.register']`). Without a
  usable `Map`/`Promise` or a per-activation record there is **no `ctx.commands`
  at all** rather than a `register` that returns nothing dispatchable.
  `ctxVersion` stays `1`.

**Keybindings and menu items can name a command instead of carrying a
callback**, which is the motivating case for replacing `win._saveToServer`:

```js
ctx.registerKeyActions([
    { id: 'save', label: 'Save', command: 'scratchpad:save' },
]);
ctx.registerWindowMenuItems(function (win) {
    return [{ label: 'Save', command: 'scratchpad:save', commandArgs: null }];
});
```

`commandArgs` (optional) is passed to the command as `args`. The two paths
differ deliberately when an item declares both forms: a key action with both
`command:` and `run:` **throws at registration** (inside `init`, where a throw
is a clean rollback and a clear message), while a **menu item** with both keeps
its `action` and does not throw — that pass runs at *render* time, where a throw
would cost the whole context menu. A command menu item defaults to
`enabled: true`. Both normalizations are identity-preserving: declare no
`command:` and the very same objects reach core.

### Authenticated HTTP: `ctx.http.fetch` (#200)

```js
const r = await ctx.http.fetch(hostId, '/info', {
    method: 'GET',   // default
    json,            // request body; sets content-type
    timeoutMs,       // TOTAL deadline — including the body read (default 15000)
    signal,          // composes with ctx.signal
});
// -> {status, json?|text?, error?}   NEVER rejects.
```

One sanctioned way for a mod to make an authenticated request to a broker —
this one or a peer. It wraps core's raw `hostFetch` (core keeps `hostFetch`
unchanged); it does not replace it.

- **`hostId` is required, and a bad one throws *synchronously*.** There is no
  same-origin default: `''`, `null`, `undefined` and any non-string throw a
  `TypeError` at the call site rather than resolving to an error value. Passing
  no host is a program defect, and a mod that swallows every result shape must
  not be able to swallow it into an accidental same-origin write. **Targeting
  this broker is spelled out**: pass the `id` of the `self: true` row from
  `ctx.hosts.list()`.
- **A `path` must be a route, not an authority.** It must begin with `/`, and
  `//…` or `/\…` throws. This is not pedantry. The URL is built as
  `(host.url || '') + path` and the self entry's `url` is `''`, so
  `'//evil/x'` is not a route on this broker — it is a protocol-relative URL
  that resolves to **another origin**, and `hostFetch` would attach this
  broker's bearer token to it. The realistic case is a mod building a path out
  of a value it did not write itself (a route name from a server response, a
  saved recording's id). `hostId` chooses the target; a `path` may never
  re-choose it.
- **An unknown `hostId` fails closed with no request issued** —
  `{status: 0, error: 'host_not_found'}`. That is a resolved value, not a
  throw, because a stale id is *data* (the user deleted a host between a poll
  and its retry).
- **It never rejects, and `status` is always present.** On a complete answer
  `status` is the HTTP status and there is **no `error` key at all** — which is
  what keeps `{status: 401, text: ''}` and `{status: 200, text: ''}` different
  objects. `error` never substitutes for an HTTP status. `status: 0` + `error`
  is reserved for the cases with no answer to report: an unresolved host, a
  transport failure, a cancel, a deadline. A route a *peer* does not have
  arrives that way too (the preflight surfaces an opaque `TypeError` with no
  status behind it), which is exactly what cross-broker callers feature-detect
  on.
- **`timeoutMs` is a TOTAL deadline, including the body read** — and that
  difference is the reason this family exists. Raw `hostFetch`'s deadline stops
  at the headers: `fetch()` resolves the moment the status line lands, so a
  route that answers and then stalls its body hangs forever, and every mod that
  cared hand-rolled its own `AbortController` around the read. `ctx.http` keeps
  its timer armed across `r.text()` (and calls `hostFetch` with `timeoutMs: 0`,
  deliberately disarming the headers-only one, so the two cannot race). A body
  that dies mid-stream is `{status: 0, error: 'TimeoutError: timeout'}` — never
  a `{status: 200}` with a truncated string. `timeoutMs: 0` (or any
  non-positive / non-finite value) opts out, and that opt-out is genuinely
  unbounded: there is no headers-only deadline left underneath.
- **JSON is parsed only when the content-type says json *and* the parse
  succeeds**; otherwise you get `text`. A body the peer mislabelled comes back
  as `text` for you to decide about, and a plain-text `42` stays a string.
- **`signal` composes with `ctx.signal`** — it is never replaced, and an
  already-aborted caller signal aborts before `fetch` is reached, so no request
  leaves a dead activation. The #198 pairing rule applies **here specifically**:
  work that must outlive the activation takes no signal. `recorder` passes it
  **nowhere**, because its save path is reached from an `onUnload` disposer; an
  already-aborted signal there would drop the segment on every
  disable-with-a-live-recording.
- **No auth side effects.** A 401 comes back *as* a 401. Nothing in `ctx.http`
  pops the auth overlay or touches the auth bookkeeping — a background poll
  opening a modal was the #161 bug class. Only an explicit user action should
  re-prompt; recovery belongs to the mod.
- A body on a `GET`/`HEAD`, and a `json` that does not serialize (including one
  that quietly stringifies to `undefined` — a function, a symbol, a `toJSON`
  returning `undefined`), also **throw**.
- **Feature-detect it:** `ctx.http && typeof ctx.http.fetch === 'function'`,
  `ctx.capabilities.http`, `needs: ['http']`. A build with no constructible
  `AbortController` cannot honour the total deadline, so it gets **no
  `ctx.http` at all** rather than one whose `timeoutMs` silently does nothing.
  `ctxVersion` stays `1`.

**Whether to declare `needs: ['http']` is a loss question, not a taste
question.** `recorder` declares it (and is `defaultEnabled`): every byte it
owns lives on a broker, so without `ctx.http` a capture would still arm, fill
memory for an hour and have nowhere to go — data loss dressed up as a working
feature. `needs` *blocks* the mod, and blocked-with-a-reason is the better
failure. Contrast `help` and `theme`, which deliberately decline a `needs` for
the surfaces they use: their loss is **partial**, and a `needs` would block the
whole mod over a degraded corner.

### Verified broker identity: `ctx.hosts.list` (#200)

```js
ctx.hosts.list();
// -> [{id, label, url, brokerId, self}]   frozen snapshot
```

A read-only snapshot of this browser's host registry. Every row is a fresh
frozen object of copied scalars and the array is frozen too, so you cannot
reach the live record through it and a later registry edit cannot mutate a
snapshot you are holding. There is **no `token`** on a row: it is not identity,
no mod needs it to key a cache, and `ctx.file` / `ctx.http` already carry it
for you without showing it. Mutation stays with core (the Hosts form) and
`ctx.hosts.invalidate`.

**Identity is two fields, and neither pretends to be the other:**

- **`id`** — the local registry handle. Stable for *this browser's* list and
  meaningless anywhere else: `'local'` here and `'local'` in another browser
  are two different brokers. Use it to address a host through the rest of the
  `ctx` — it is what you pass to `ctx.http.fetch`.
- **`brokerId`** — cross-URL identity: the broker *said* this, over `/info`,
  and it is the same string under every address that broker answers on. Use it
  as a cache key, or to notice that two rows are one machine.

**`brokerId` is `null` until the host has answered.** Not a provisional hash of
the URL, not the URL, not the `id` — `null`. A guess later swapped for a
verified value is worse than an absence: every cache keyed on the guess
survives the swap while pointing at the wrong broker. "Unverified" is a real
state (an older broker with no `/info`, a wrong token, a host that is simply
offline), it can last the whole session, and **a caller branching on identity
must handle `null` explicitly rather than falling back to the URL** — that
fallback is precisely what `update`'s old `hostFingerprint = host.url` did
wrong. The same broker at `127.0.0.1`, `localhost` and its Tailscale address is
three "fingerprints"; a URL repointed at a *different* broker keeps the same
one.

**A URL repoint resets `brokerId` to `null`, and it is never carried over.**
Carrying it would label a new — possibly hostile — endpoint with the old
broker's verified identity. After a repoint the field goes null and then either
comes back the same (one broker under a second address) or comes back
*different* (a genuinely different broker) — the transition URL-as-fingerprint
could not represent at all.

**Neither field is ever a merge key for registry entries.** That is #174's
lesson and it cost a host-repoint vector: matching an *incoming* entry's `id`
against a local record and then updating the local URL from it let anyone who
could write the shared blob re-point a broker entry at a machine they
controlled. `brokerId` is no safer for that job — it is a string a remote
endpoint chose to say. Both fields are for **change detection and cache keys on
this page only**; a mod that syncs host records must key its merge on something
the user authorized.

**Exactly one row has `self: true`** — the local broker (`id` `'local'`, `url`
`''`). Its `brokerId` is learned by a different path from every other row, but
the same null-until-answered rule applies, so no "is this me" branch is needed
beyond the flag:

```js
function selfHostId() {
    for (const h of ctx.hosts.list()) if (h.self) return h.id;
    return null;
}
```

`list` is a **member** of the existing `hosts` family (#195 created it for
`invalidate`), so it owes no capability entry of its own: feature-detect with
`typeof ctx.hosts.list === 'function'`, or declare the finer
`needs: ['hosts.list']`. `ctxVersion` stays `1`.

### Watching a terminal: `info.tapOutput` / `tapInput` / `onResize` / `onModesChanged` (#201)

These are members of the `info` bag `ctx.windows.onTerminalCreate` hands you.
They are the **sanctioned replacement for monkey-patching an xterm instance**:
before them, a mod that wanted to see terminal traffic replaced `term.write` by
assignment, a scheme whose own comment in `mods/recorder/recorder.js` admits
two patchers cannot coexist.

```js
ctx.windows.onTerminalCreate(function (info) {
    const off = info.tapOutput(function (data) { /* bytes that were written */ });
    info.tapInput(function (data) { /* bytes that were sent */ });
    info.onResize(function (d) { d.cols; d.rows; });
    info.onModesChanged(function (modes) {
        modes.mouseTracking;   // 'none'|'x10'|'vt200'|'drag'|'any'
        modes.mouseActive;     // mouseTracking !== 'none'
        modes.altScreen;       // the alternate buffer is up
    });
});
```

Each registrar returns an idempotent `off()`. A non-function argument is a
silent no-op that still hands back a callable `off()` — the posture
`onTerminalCreate` itself takes.

**They are observers, and that is enforced rather than requested.** Every tap
fires strictly *after* the underlying write or send has been dispatched, and
the output dispatch sits inside the same `try` as the write — so a write that
**threw** never reports its bytes as delivered. A mutable payload is copied
**per tap**, so tap #1 cannot rewrite what tap #2 observes: a typed-array view
is `slice()`d, and so is an **array**, which is the case that mattered — the
JSON output frame is server-supplied, so `data.data` can be `[65]`, and xterm
is still holding that same array for its asynchronous write queue. A payload
that is neither string, view nor array is handed out as **`null`** rather than
as a live reference; "I cannot show you this safely" is truthful where a shared
reference is not. Taps run in registration order over a snapshot with a
liveness recheck, isolated per callback: a throwing tap loses only the rest of
its own callback, and the terminal never sees the throw.

**`tapInput` fires per wire frame, after the send.** Input is chunked into
≤256 Ki-char frames before it goes out, and the tap fires once per frame, so it
observes what actually left rather than what was about to. That is also what
makes it cover the hand-bracketed ConPTY paste path (#138), which goes out
through the same `sendChunked('input', …)` call and not through `term.paste()`.

**No history replay.** `onTerminalCreate` replays over terminals that are
already open, so a tap registered from that replay sees traffic **from then
on**. Reconstructing the scrollback that came before stays the recorder's job
(its serialize-addon keyframes) — do not expect a tap to hand you the past.

**`onResize` is fed from xterm's own resize event**, not from core's
`term.resize(cols, rows)` call site. Both spellings would cover the
broker-confirmed resize, but only this one also covers a **mod** calling
`fitAddon.fit()`, which resizes inside xterm with no core call site involved.
It fires once per applied resize whichever path drove it, and each tap gets a
fresh `{cols, rows}`.

**`onModesChanged` replays the current snapshot synchronously on subscribe.**
Otherwise every subscriber would have to re-invent an initial sample, which
matters because `onTerminalCreate` replays over terminals already running an
app that may not write again for minutes. A terminal that has never seen a mode
reports the real read of xterm's defaults (`'none'` / `false` / `false`). An
**unreadable** getter replays **nothing at all** — announcing `'none'` for a
terminal that may well be mouse-active would make the next successful sample
report a transition out of a state core never observed. A subscription on an
already-disposed terminal is refused outright.

**It fires on group transitions, not per-flag flips.** xterm's `DECRST` of any
mouse-tracking mode clears the whole group — the vendored build keeps a single
active protocol, not independent 1000/1002/1003 flags — so per-flag events are
not expressible here. `mouseTracking` *is* the group's resolved value:
`'vt200'` → `'any'` is one event, and any `DECRST` that drops the group is
exactly one event to `'none'`. It follows for free that `RIS` (which resets the
mouse service) reports a transition to `'none'` while `DECSTR` (which does not)
reports nothing — the sampler reads **state** after the parse and never
interprets the escape bytes.

**The honest limit: it is coalesced, and it is a state notification, not an
event log.** The sampler only *arms* a `requestAnimationFrame`, so two changes
inside one frame collapse into a single delivery of the final state; and
`requestAnimationFrame` does not run in a hidden tab, so any number of changes
made while the tab is hidden collapse into one delivery when it is shown again.
The subscriber sees the final state, never the intermediate ones. Because these
events describe persistent state, a missed intermediate is not a lost fact —
but **nothing here is a suitable trigger for one-shot side effects.**

**Lifecycle: auto-removed at terminal dispose *and* at mod teardown**, both
armed at the moment of registration. `win.cleanups` fires only on window
*close*, so the teardown half is what stops a disabled mod from leaving taps
running on every terminal that is still open.

**Both mechanisms coexist for one release, deliberately.** Core hooks its **own
call sites** and nothing here touches `term.*` by assignment — `onModesChanged`
is a subscription to xterm's public `onWriteParsed`, `onResize` to its public
resize event. That is a compatibility requirement, not taste: recorder checks
"is `term.write` still the function I installed" before restoring it, and a
core wrapper on the instance would make that check false and corrupt its
restore path. One consequence worth stating: a tap never sees anything a mod
writes straight to `win.term` (recorder's own playback included), because that
is not a core call site. `docs-terminal-funnels.md` enumerates the boundary and
its named limits.

### Cell size and font: `info.cellDims` / `info.setFont` / `ctx.terminals` (#201)

```js
ctx.windows.onTerminalCreate(function (info) {
    const d = info.cellDims();          // {width, height} | null
    info.setFont('Iosevka, monospace');
    info.setFont(null);                 // drop MY override
});
ctx.terminals.defaults.fontFamily;      // the core baseline, frozen
```

**`cellDims()` returns `null` before the first render, and on a disposed
terminal.** There is no fallback and there must never be one: the hardcoded
9×17 that `mods/recorder/recorder.js` falls back to is the anti-pattern this
replaced — that is not a measurement, it is a number that happens to be close
on one machine. A zero width or height is *pre-render*, not a reading, and is
reported as `null` too. A disposed terminal answers `null` even though xterm can
leave its last dimensions object resident, because a stale measurement
presented as a current one is the same class of lie. **A caller that needs a
cell box before anything has rendered has to handle `null`.** Asking is also
non-destructive: nothing is cached into settings on the way out.

**`setFont` is last-writer-wins *with an owner record*.** Core keeps, per
terminal, the ordered list of which mod set which family; the last entry is
what is on screen. Ownership is by **mod id**, so calling `setFont` twice on one
terminal updates your own entry rather than stacking. The record is the whole
point — it is what makes the **revert chain** correct:

- disabling the **last** writer reverts the terminal to the **earlier
  surviving** writer;
- disabling **all** writers reverts it to `ctx.terminals.defaults.fontFamily`;
- `setFont(null)` (or any empty / non-string family) is the same code path as a
  teardown, so a mod cannot get a revert core's own unload does not also get.

A plain "remember the previous value and restore it on unload" scheme — which
is what a mod can implement for itself — gets both interesting orders wrong:
two font mods on one terminal either strand a dead mod's font on screen or
stomp a live mod's font back to the baseline, depending on which one is turned
off.

**A dead activation cannot take ownership.** `setFont` from a stray callback
after your mod was disabled is refused, because teardown's release is one-shot:
re-creating the entry afterwards would leave a live terminal wearing a dead
mod's font permanently. The release is armed on both ends (dispose *and* mod
teardown) at the moment the override is set.

Core owns the apply: the family is written to `term.options` and followed by
core's own refit, once, so the revert path is the same code as the set path and
cannot drift from it.

`ctx.terminals` is a **new top-level family** and carries its own capability
entry (`terminals`, version 1) — feature-detect with `ctx.terminals`, or declare
`needs: ['terminals.defaults']`. The four `info` members above are members of
the existing v1 `windows` family and owe no capability entry of their own:
feature-detect with `typeof info.tapOutput === 'function'`. `ctxVersion` stays
`1`.

### Queued modal prompts: `ctx.dialog.open` (#202)

```js
const d = ctx.dialog.open({
    title: 'Unlock registry',
    fields: [{ name: 'pass', label: 'Passphrase', type: 'password' }],
    submitLabel: 'Unlock',          // default 'OK'
    cancelLabel: 'Not now',         // default 'Cancel'
});                                 // the handle comes back SYNCHRONOUSLY
d.replace(spec);                    // re-spec THIS dialog
d.close();                          // dismiss THIS dialog
const vals = await d.result;        // {pass: '…'} | null (dismissed)
```

A field is `{name, label, type, value, placeholder}`. A field with no string
`name` is skipped. `type: 'select'` renders a `<select>` from `options`
(`'a'` or `{value, label}`); `type: 'password'` renders a real password input;
**every other `type` falls back to `text`**, so a spec cannot smuggle an
arbitrary input type onto the page. `result` resolves to an object of
`name -> string` on submit, and `null` on cancel, dismissal, or any teardown.

**Mod-facing opens ALWAYS queue.** There is no cancel-the-current-dialog mode
on this surface at all — not a flag, not an option — which is the whole point:
one mod can never dismiss another mod's prompt. A second `open()` while one is
on screen waits its turn and is shown when the first settles. Core's own
`openDialog` is a singleton that *cancels* whatever is live, and mods that
prompt through it have to carry single-flight flags (`mods/host-registry`'s
`_encBusy`) to stop themselves silently dismissing their own first prompt.
That workaround class is what this surface removes.

**The named limit: core still wins.** `openDialog` is unmodified, so a *core*
dialog still cancels whatever is live, including a mod's — and that mod's
caller reads `null`, indistinguishable from a user dismissal. The asymmetry is
deliberate: core is the application, a mod is a guest. Its consequence is that
the queue also refuses to show anything while a foreign (core) dialog is up; it
waits politely rather than cancelling it.

**The handle is returned synchronously, before display**, so `close` and
`replace` exist for the whole queued life of the dialog and not only once it is
on screen — and each acts on **this** dialog only. Closing a queued entry drops
it (it is never shown, and its `result` resolves `null`); closing the shown
entry finishes only that dialog; `replace` on a queued entry just swaps the
spec it will eventually be shown with, and on the shown one re-renders it
without ever releasing its turn, so a `replace` cannot let another mod's queued
dialog jump in front of it.

**A dead mod never wedges the queue.** On teardown that mod's queued entries
are dropped and its on-screen dialog is closed, so the next owner's entry is
shown immediately; every dropped entry resolves `null`, the same value a
dismissal produces. An `open()` from an already-dead activation is refused
outright (a handle whose `result` is already `null`) rather than queued, so a
dead mod's dialog cannot flash on screen.

**Password values reach exactly one place: the caller's `d.result`.** Core
never logs them, never persists them (no prefs / state write lives on this
path), never puts them in the help corpus, and drops its own reference to the
inputs the moment the dialog settles. A commit that races a teardown resolves
`null` rather than a half-read object.

`dialog` is a **new top-level family** with its own capability entry
(`dialog`, version 1) — feature-detect with `ctx.dialog`, or declare
`needs: ['dialog.open']`.

### Introspection: `ctx.mods`, `ctx.settings.describe`, `ctx.helpCards` (#202)

```js
ctx.mods.list();                    // [{id, active, pin, version, tiers}]
ctx.mods.isActive('git');           // true | false
ctx.mods.pinOf('git');              // true | false | null
ctx.settings.describe('git', 'branchStyle');   // {type, options, default}|null
ctx.settings.describe('branchStyle');          // own-mod shorthand
ctx.helpCards.list();               // the sanitized typed-span card DATA
```

These replace the `window.__mods` scraping that `mods/mod-sync` and `mods/help`
do today. **`window.__mods` is a test fixture, not a contract** — it is the
loader's own mutable bag, and it carries live records (unload arrays, control
`read`/`reflect`/`onChange` closures, live DOM sections). Reading it reaches
into core's internals and pins every field of them as a de-facto API.

**Everything here returns a fresh, FROZEN clone, built per call out of
primitives.** Fresh means a caller cannot reach core state through what it got;
frozen means a mutating write is a visible no-op rather than a local copy that
silently drifts from the truth. The next call returns a different object graph,
so nothing is shared between two callers and nothing is cached. **The shapes
are contract now**: adding a field later is additive, changing or removing one
is a break — so they are deliberately minimal. No `unloads`, no `section`, no
closure, no `Map` and no DOM node ever leaves this surface. `mods.list()`
likewise omits `ctxVersion`, `defaultEnabled`, `requires` and `init`.

**`describe` is two-arg**, `describe(modId, key)`, because the caller must be
able to name *whose* setting it is asking about — the motivating caller
describes other mods' settings, never its own. `describe(key)` is the own-mod
shorthand and is chosen by **argument count** (exactly one), so
`describe(a, undefined)` is a two-arg call with no key and answers `null`.
It returns `null` for an unknown key, an unknown mod, or a mod that is **off**
(a disabled mod has no mounted control, which is also the answer the Control
Panel gives) — never a throw and never a half-filled object with a made-up
default.

**A `text` descriptor's `options` are SUGGESTIONS, not a domain.** They are
reported because the pane renders them; a caller must not use them as a
validation set. The `type` says which it is: `'text'` is the one kind whose
options do not bound it. For `radio` / `select` / `combo` the options *are* the
domain, and `default` is the declared fallback the primitive itself would use.

**Introspection is LOCAL-PAGE ONLY.** It answers about *this* page: the mods
this build registered, the pins *this* broker resolved at boot, the controls
mounted in *this* Control Panel. A remote broker's pins still come off the wire
(`GET /mods/policy`) and must keep doing so — there is no remote form of any of
this on purpose, because a getter that silently answered about a different host
is the worst possible shape for a sync tool.

Two limits worth reading twice:

- **`pinOf` returning `null` means "no pin in force", NOT "no such mod".** An
  unpinned known mod and an id nothing ever registered answer identically, as
  the loader's own pin lookup does. Existence is `list()`'s question.
- **An installed-but-not-loaded package is ABSENT from `list()`**, not listed
  as inactive: `list()` enumerates what actually registered on this page, so a
  package that 404'd, cycled, or registered the wrong id simply is not there.
  The union catalog/status model is a different question with a different
  answer, and a frozen contract may not blur the two.

`list()[i].active` and a later `isActive()` for the same id may disagree —
both read live state at call time, and a snapshot is a snapshot.
`helpCards.list()` reads the card registry live, so a card contributed by a mod
that has since been disabled is gone (`ctx.registerHelpCards` splices its own
entries out on teardown). Every card is re-coerced to strings on the way out,
so what you get is structured-cloneable data.

`mods` and `helpCards` are **new top-level families** with their own capability
entries (version 1 each). `settings.describe` is a new **member** of the
existing v1 `settings` family and owes no entry of its own — feature-detect
with `typeof ctx.settings.describe === 'function'`, or declare
`needs: ['settings.describe']`. `ctxVersion` stays `1`.

### Anchored popovers: `ctx.popover.anchor` (#202)

```js
const p = ctx.popover.anchor(node, anchorEl, {
    placement: 'bottom-start',   // | bottom-end | bottom | top-start
                                 // | top-end | top   (default bottom-start)
    gap: 2,                      // px between anchor and node, clamped 0..64
    onClose: function (why) {},  // 'close' | 'outside' | 'escape'
                                 // | 'anchor-gone' | 'node-gone'
                                 // | 'teardown'
});
p.close();          // acts on THIS popover only
p.reposition();     // re-measure now (after a synchronous fill)
p.isOpen();         // false once anything has dismissed it
```

Core appends the node to `document.body`, gives it the `mod-popover` class and
`position: fixed`, measures it off-screen for one frame so it is never painted
at 0,0, and **removes the node itself** when the popover closes — a mod that is
gone is in no position to remove anything.

**Anchoring hands the node to core**, and that is unconditional: a node that
already lives inside your own window is *moved* to the body, not left where it
was. This is not tidiness — placement is in **viewport** coordinates, and only
`html`/`body` are guaranteed not to be a containing block for `position: fixed`
descendants, so a node left inside a transformed ancestor (an app window
mid-drag has one) would be positioned against the wrong origin. Build the node
detached and hand it over; do not expect it back where you put it. The
symmetric half is that core removes what core inserted.

The popover also closes with `'node-gone'` if **your** node leaves the page
while the anchor is still on screen. Without that check the entry would be
immortal — `isOpen()` true, `onClose` never called, the document listeners
still bound, and the frame loop measuring a detached element for the life of
the page. The handle comes back synchronously
and *always*: a refused anchor (dead activation, missing node or anchor)
answers with an already-closed handle rather than a `null`, so a caller never
has to branch on whether it got one.

**There is NO motion at all** — no entrance transition, no exit transition, no
keyframe, and nothing in this range reads `matchMedia`. This is not "animate
unless `prefers-reduced-motion`", deliberately: anything gated on that query is
simply *instant* for a viewer who has it set, so it reads as broken-or-fine
depending on who is testing (the precedent is stated in `15_css_dialogs.css`).
A popover appears and disappears, identically for everyone.

**It re-measures every frame while at least one popover is open**, which is one
loop instead of a `resize` listener, a capture-phase `scroll` listener and a
`ResizeObserver` — three partial answers to the same question. That single pass
is what covers a window being **dragged** (which fires neither resize nor
scroll), an inner container scrolling, and a node the mod refilled after
anchoring. It costs nothing when nothing is open (the loop is not running) and
nothing in a background tab (rAF does not fire).

Placement **clamps with a FLIP**: a bottom-placed popover that would run off
the bottom is put *above* the anchor instead (and vice versa), because sliding
it up would cover the very control it points at; only the remaining overflow is
clamped, to a 6px viewport inset. The horizontal axis is a pure clamp.

**Dismissal is judged per popover.** Outside-click uses one document listener
for the whole page and tests each popover against **its own** node — with two
mods' popovers open, a click inside one closes the *other* and leaves the one it
landed in. The anchor counts as *inside*, so a toggle button does not
close-then-reopen and never close. Escape closes the **topmost** popover only
and swallows the key, so stacked popovers peel one press at a time. An anchor
that leaves the page closes its popover with `'anchor-gone'` rather than
leaving a box floating at coordinates that no longer mean anything.
`anchor()` on a node that is already in an open popover **replaces** that
entry — two entries would fight over the same `left`/`top` every frame.

Teardown is per **mod activation** (`ctx.onUnload`), not per window: a popover
is not owned by a window at all — a taskbar-dot preview has no window anywhere
in the chain — so every popover an activation opened closes on a disable, a
reload and an uninstall alike, with `why === 'teardown'`.

`popover` is a **new top-level family** with its own capability entry
(`popover`, version 1) — feature-detect with `ctx.popover`, or declare
`needs: ['popover.anchor']`.

### A mod's own files: `ctx.assets.url` (#202)

```js
const u = ctx.assets.url('sprites.css');
// installed mod: '/mods/<id>/<gen>/sprites.css'
// shipped mod:   undefined   -- feature-detect the RETURN VALUE
if (u) { /* fetch it, inject it, hand it to a worker */ }
```

An installed mod already has a content-addressed serving pipeline behind it —
`GET /mods/<id>/<gen>/<name>`, the same route the loader uses to inject that
package's scripts and styles — and until now no handle on its own bytes.
`ctx.assets.url` is that handle, and it rides the **existing** generation
machinery: the `<gen>` comes from this page's load record and the URL is built
by the same builder the `<script src>` injection uses, so a mod's asset URL and
its script URL can never disagree.

**A shipped mod gets `undefined`, on purpose.** A shipped mod is spliced into
the one inline `<script>` at assembly time and its files live in the shipped
tree, which no route serves per file. Adding one would extend forced-public
serving to the shipped tree — a new exposure decision that gets its own issue,
not a side effect of this one. Faking it with a relative path would be worse:
that "works" in a hand test and 404s in the page. **The `assets` family is
present either way**, for shipped and installed mods alike, so feature-detect
the **value** and not the family — otherwise every caller writes two detections
for one fact.

**Never persist a URL across an upgrade.** The `<gen>` segment *is* the
package's content hash and the route serves `immutable, max-age=31536000`, so
an upgrade publishes a *different* URL and the old one stops resolving once
that generation is reaped. A URL stashed in `ctx.prefs`, `ctx.storage` or
`/state` keeps working right up until the mod is updated and then 404s with no
error anyone can attribute. Call `url()` at the point of use, every time — it
is a property read and a string concat, and there is nothing to memoize.

**It answers only names the route will serve.** The rule is **membership**, not
sanitisation: a name is answered only when it is a key of the package's own
`integrity` map — which the broker builds from that generation's files filtered
by content type — so an unservable name is unrepresentable rather than defended
against. A name grammar (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`, plus a `.js`/`.css`
suffix) refuses first, before the map is consulted, because the route is
forced-public and a plausible-looking URL that escapes the package directory is
the case worth two refusals. Everything below is `undefined`, uniformly, never
a throw:

- traversal or any embedded slash (`'../../etc/passwd'`, `'a/b.js'`), and a
  leading slash;
- a query string or fragment (`'a.js?v=2'`, `'a.js#top'`);
- an empty name or a non-string — there is **no coercion**, so `url({})` cannot
  build `'…/%5Bobject%20Object%5D'`;
- a name the package does not ship, or one it ships that the route cannot serve
  (`help.md`, `mod.json`);
- a package with no usable generation.

There is deliberately **no `url(modId, name)` form**: the lookup is keyed by
the ctx's own id, so a mod can only ask about its own package. One mod
addressing another's assets is a different feature with a different exposure
argument.

**The route is forced-public: no secrets in assets, ever.** A `<script src>`
cannot carry an `Authorization` header, so `/mods/<id>/<gen>/<name>` serves
without a token by design. Every byte a mod ships there is readable by anyone
who can reach this broker and knows the id, the gen and the file name — ship
code and static data, never a credential, a host list, or anything derived from
one. And because the answer comes from *this* page's boot record, a package
uninstalled or upgraded mid-session still answers with the generation this page
loaded (the running code *is* that generation); the bytes may nevertheless be
gone from the store, so a fetch of a returned URL can 404 at any time — which
is the other half of why you must not persist one.

`assets` is a **new top-level family** with its own capability entry (`assets`,
version 1) — but a shipped mod has the family and no URLs, so
`needs: ['assets.url']` gates on presence, not on usefulness. `ctxVersion`
stays `1`.

---

## 9. Testing a mod (#203)

Every ingredient below already ships. This is the recipe, written down once,
with its traps named — apply it to a shipped mod or an installed one; nothing
here is install-only.

1. **Start an alt-port broker with `cwd` = repo root, or `PYTHONPATH` pointing
   at it.** If neither holds, `import webterm` can resolve to a *different*
   install on `sys.path` — some other checkout, or a package installed into
   the interpreter — and the broker you just started serves **that** copy's
   assets. You edit a fragment, reload, see nothing change, and the instinct
   is to suspect the edit. It is almost always this.
2. **`INDEX_HTML` is built once, at import** (`ui.py`'s `assemble()`, held at
   module scope rather than per-request — `ui.py:619`, `INDEX_HTML =
   assemble()`). A fragment edit on disk is invisible to an already-running
   broker; it needs a restart. Before debugging anything else, `curl` the
   page you are testing against and `grep` for the string you just edited —
   if it is not there, you are looking at stale bytes, not a bug. This one
   check saves more time than everything after it combined.
3. **Navigate with the auth token and `?nomods=1`.** The flag makes
   `loadMods()` return before it fetches or inits a single mod
   (`86b_js_mod_packages.js:38-43`, `_nomodsRequested`) — zero catalog
   fetches, zero `registerMod` calls, a clean desktop with nothing riding on
   load order or another mod's side effects. Start every test page here, not
   on the real desktop with 19 shipped mods already up.
4. **Drive the fixture through `window.__mods.__test.run`, not a parallel
   harness:**
   ```js
   const rec = window.__mods.__test.run({
       id: 'x-fixture',
       ctxVersion: 1,
       init(ctx) { /* … */ },
   });
   ```
   `run` is a thin call onto `initMod` (`86_js_mod_loader.js:2432`) — the
   exact function real boot and a live install both call. There is no
   separate "test mode" `initMod` to drift out of sync with the real one; a
   fixture that passes here passes for the reason production code would
   accept it, not because a stand-in was lenient.
5. **Assert through the read-only inspectors** — `isActive(id)`,
   `statusOf(id)` (name it as such; the Mods-pane status/`warnings` shape),
   `themeSubscribers()` — and tear down with `disable(id)`, then assert the
   fixture's `onUnload`s actually ran (a dropped subscriber count, a removed
   DOM node, whatever the fixture registered). **Do not use `run`/`disable`
   to test persistence or the theme/announcement channel.** Both bypass
   `setModEnabled` and the post-cascade announcement pass *by design* — they
   exist to drive `initMod`/`disableMod` in isolation, not to exercise the
   enable-persistence or notification paths a real toggle goes through
   (`86_js_mod_loader.js:936-940`). A claim about *persistence* (does the
   pick survive a reload / sync across tabs) or about the *announcement
   channel* (does `ctx.theme.onChange` fire, does a subscriber count move)
   needs `__test.setEnabled(id, on)` or an actual Mods-pane click instead —
   those ride `setModEnabled`, the path a real operator uses.

`?nomods=1` earns a second mention here for the same reason §11.3 gives it
one: it is written up elsewhere (§11.3) purely as the rescue hatch for a
bricked desktop. Steps 3-5 above are what make it a *testing* tool as well —
the same read-only, never-persisted flag, used deliberately instead of
stumbled into.

## 10. Help: `help.md` and the regen you must not skip

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

## 11. Publishing an installed mod

### 11.1 The `x-` namespace rule

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

### 11.2 The portable-mod contract

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

### 11.3 Installing takes effect on the next page load — always

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

### 11.4 An installed mod's code and styles are public

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

### 11.5 The install API

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
anything, the same `blank_js_literals` machinery the portable-mod lint (§11.2)
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

### 11.6 Status vocabulary

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

## 12. Traps that have actually bitten this codebase

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
preference. Use `ctx.prefs` (or `ctx.storage`) for genuinely per-browser state,
and `isBrowserGlobal` only controls which Control Panel tab shows the widget.

**A `saveChain` flush from `ctx.onUnload` is a no-op.** The chain drops its
pending batch the instant a teardown starts, so a "flush on the way down"
registered as an unload disposer saves nothing and resolves
`{ok:false, error:'unloaded'}` — by design, because a disable is synchronous and
cannot await a network write, and one that landed anyway would clobber the next
activation. Put the deliberate final flush somewhere the mod is still alive: a
window-close cleanup, or an explicit save action (§8).

**`ctx.signal` on a close-time save aborts the save.** The signal exists to kill
work that must *not* outlive the activation, so threading it through every call
reflexively is exactly wrong for the one path that is meant to survive — the
deliberate final flush and the writes around it (§8). If it has to land after
the teardown starts, it does not take the signal.

**A reducer is re-run, so it must not *do* anything.** `serverStore.update` and
`saveChain.save` invoke your `fn` once per attempt, and a `409` means another
attempt. A side effect in there — a counter, a toast, another `save()` — runs
once per attempt, and it is the one part of the CAS contract nothing can check
for you.

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

## 13. Checklist for a new shipped mod

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
      `ui._MAX_LINES`, with **prefixed** selectors (§14).
- [ ] Optional `help.md` + the `help` block — then run
      `python -m webterm.broker.help_corpus`.
- [ ] `python -m pytest tests -q` clean, and the page loaded in a real browser
      with the console open.
- [ ] Restart the broker before you believe anything you see.

---

## 14. CSS: what you own, what you may use, what is private

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
