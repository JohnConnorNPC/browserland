# Browserland Mod Support — Current State and Next-Version Requirements

**Audience:** project owner, planning the next major version of the mod platform.
**Sources:** `86_js_mod_loader.js` (2491 lines), `86b_js_mod_packages.js`, `90_js_mod_boot.js`, `84_js_active_view_lifecycle.js`, `64_js_sessions_poll_control.js`, all 19 mods under `webterm/broker/mods/`, broker routes in `webterm/broker/app.py`, `wiki/Writing-a-Mod.md`, `wiki/Installing-Mods.md`, `wiki/Technical-Reference.md`.

---

## 1. Executive summary

Browserland's mod system is a mature, honest, and internally consistent **trusted-first-party plugin architecture**: mods are same-realm scripts concatenated (or `<script src>`-loaded) into the broker page, `ctx` is explicitly "conflict-avoidance + review hygiene, NOT a security boundary" (86:1-9), and the trust event is the install, not the fetch (app.py:6263-6267). Within that model, the loader is genuinely strong — init fault isolation with rollback, LIFO teardown, topo-sorted dependencies with cascade bring-up/take-down, broker pins, runtime package install with content-addressed generations and SRI, and a well-documented authoring contract.

The audit of all 19 mods, however, shows the ctx surface covers a **minority of what mods actually do**. Every mod except mousemode reaches directly into hoisted core globals (`windows`, `sessions`, `prefs`, `frontId`), core private state (`_stateReady`, `_deactivated`, `_modFileApi`, `modCatalogCache`), and duck-typed `win._*` convention hooks. Nine mods hand-build an identical ~30-field app-window scaffold. Three mods independently reimplement serverStore compare-and-swap. Cross-mod calls happen through `typeof`-guarded hoisted globals. The shared-scope concatenation format is the root cause of the TDZ landmines, the 2500-line fragment cap pressure (update.js sits at 2496/2500), the `.cap` capability-smuggling idiom, and the impossibility of live install/reload.

Security-wise, the model is candid but has three real, actionable exposures: `/status/fetch` egress has no operator gate (token-only); `/mods/install` and `/mods/policy` are token-gated but not lease-gated, so **any running mod can install another mod** with the token the page already holds; and the host-registry mod stores broker tokens in a `/mod-store` revision ring that retains plaintext history. Browser-side egress is entirely ungoverned by CSP.

**Headline recommendations for the next major version:**

1. **`ctx.windows.createAppWindow`** — one API deletes the largest coupling surface in the codebase (9 mods).
2. **A `ctx.events` family** (host auth/changed/removed, state adopted, lease changed) — kills the `win._*` duck-typed protocols.
3. **Spend the security budget at the two real edges** — network (CSP pinned to self + registered hosts; server-side egress gates modeled on #182's three-gate pattern; admin-token or lease gating on install/policy routes) and supply chain (package signing, install-time capability lint). Explicitly reject in-realm sandboxing: it would break every mod except mousemode, clock, and pattern.
4. **Migrate the module format to per-mod ES modules** as the platform's structural bet — it fixes identifier collisions, TDZ, the fragment cap, `.cap` smuggling, cross-mod imports, and unlocks hot reload via dispose-then-reimport of content-addressed generations.

---

## 2. Current mod ecosystem overview

### 2.1 Trust model and distribution shapes

- Mods are **trusted first-party code**, not sandboxed (86:1-9). A same-origin mod already holds the page's auth token; the only real controls are review and the broker's `mods_enabled` master switch.
- **Two distribution shapes** (Writing-a-Mod §10):
  - **Shipped** — spliced by `ui.py` into the single inline `<script>` alongside core fragments; change requires a broker restart.
  - **Installed** (#163) — `x-`-prefixed ids, grammar `[a-z0-9][a-z0-9-]{0,63}`, served as separate classic `<script src>` tags from content-addressed URLs `/mods/<id>/<gen>/<file>` with SRI; installed via `POST /mods/install` (2 MiB cap, in-memory validation, atomic generation commit, one prior gen retained); **applies on next page load** — a JS-semantics constraint, not a missing feature (86b:10-19).
- `?nomods=1` is the non-sticky rescue hatch (86b:27-43).

### 2.2 The 19 mods

| Mod | Lines | Role | ctx footprint (relative) |
|---|---|---|---|
| aistatus | 473 | AI-provider status chip + window | medium |
| clipboard | 293 | clipboard history window | medium |
| clock | 175 | taskbar clock chip | high (cleanest, no core-global reach) |
| editor | 2217 + 245 | text editor (vendored CodeMirror) | low relative to size — heavy core reach |
| file-manager | 1757 | file browser, cross-host transfer | heavy `ctx.file` user, heavy core reach |
| git | 339 | per-terminal git status widget | medium (uses `onTerminalCreate`, `ctx.session.git`) |
| help | 717 | Help window + corpus | mostly hoisted core-reachable code by design |
| host-registry | 1936 | shared broker list w/ encryption | serverStore + heavy core-cache reach |
| mod-sync | 1237 | cross-broker mod policy sync | **one** ctx call; everything else core/loader internals |
| mousemode | 225 | mouse-mode terminal chip | ctx-only — the cleanest windowed mod |
| pattern | 112 | desktop background pattern | small; deliberately exports globals |
| recorder | 2169 | session recording/replay | monkey-patches xterm; heavy |
| scratchpad | 681 | server-synced scratchpad | serverStore + editor's CodeMirror global |
| sticky | 212 | sticky-note taskbar behavior | hand-rolled chips |
| task-manager | 649 | process viewer/killer | `ctx.session` via `.cap` stash |
| termfont | 156 | terminal font setting | duplicated core constant |
| theme | 114 | theme palette | cross-mod global call into pattern |
| update (3 files) | 546+383+2496 | fleet update/restart UI | raw `hostFetch` to 5 routes; **2496/2500 cap** |
| workspaces | 1108 | virtual workspaces | richest ctx.desktop consumer; also writes `frontId` |

### 2.3 Loader mechanics

- **Registration:** `registerMod({id, init, version, ctxVersion, tiers, defaultEnabled, requires})` called synchronously at page-script eval (86:98-191). Duplicate id → `ModConflictError`; `ctxVersion` must match exactly (loader = 1) or init is refused; `tiers` is **display/review only, not enforced** (86:143-152); `requires` blocks init until dependencies are active (#121). Installed packages are id-bound via `data-mod-package` + `document.currentScript` — convention, not boundary (86:109-132).
- **Fault isolation:** an init throw is caught, partial init reversed via LIFO `_runUnloads`, slot released; siblings and core unaffected (86:1955-1961). Teardown callbacks individually try/caught (86:206).
- **Enable/disable layering:** broker pin (`/info.mod_policy`, #157) > per-browser `webterm:mods:disabled` XOR override (deliberately not synced) > manifest `defaultEnabled` (86:2043-2048). Pins transitively imply a pinned mod's `requires` (86:1848-1868). `_takeDown` computes the transitive dependent closure and tears down dependents-first (86:2086-2099).
- **Consent signal:** `ctx.enabledByUser` (#182) is set only when init came from the Control-Panel checkbox click (86:1938-1953) — "a click is consent; an init is not." Not propagated to dependency cascades.

### 2.4 Boot flow

1. Shipped mod scripts `registerMod` synchronously during eval; `90_js_mod_boot.js:17` calls `loadMods()` last, with `restoreAppWindowsAfterMods()` chained on both fulfillment and rejection so a mods-off/failed browser never loses window restore.
2. `loadMods` (86:2188-2312): nomods check → `localInfo()` (policy, catalog, master gate — fail-open default-on, 86:1793-1823) → `_loadInstalledPackages` (parallel packages, serialized scripts per manifest, 5 s proceed-anyway deadline, SRI) → topo sort (Kahn + Tarjan cycle split, 86b:357-415) → pin resolution → Mods pane mount → forward init loop, each isolated → repaint + `notifyModTheme()`.
3. Restore race (#167, 84:45-193): unregistered-kind window records deferred and retried by `restoreAppWindowsAfterMods`, bounded 8 rounds.

---

## 3. The ctx API today — full reference

### 3.1 Client-side ctx (ctxVersion 1, all additions since additive/feature-detected)

| Family | Members | Notes | Where |
|---|---|---|---|
| **Identity/lifecycle** | `ctx.id`, `ctx.ctxVersion`, `ctx.enabledByUser`, `ctx.onUnload(fn)` | LIFO teardown, each callback isolated | 86:214-220, 1938-1953 |
| **Visibility** | `visibility.pausableInterval(fn, ms)` → `{stop}`, `visibility.onVisibility(fn)` | interval ×10 slower while hidden; coalesced catch-up tick; auto-teardown | 64:26-72, 86:216 |
| **Storage (local)** | `storage.get/set/remove` | namespaced localStorage `webterm:mod:<id>:<key>`, per-browser, not synced, exception-swallowing | 86:223-234 |
| **Storage (server)** | `serverStore.get(opts)`, `set(value, baseRev, opts)`, `getRevision(n, opts)` | per-broker per-mod KV at `/mod-store/<id>`; optimistic rev CAS, 409 inlines live value; 50-deep revision ring; `purgeRevisions` (#65); `opts.host` fails closed | 86:388-461 |
| **File I/O** | `file.read/write/list/delete/upload/readChunk/hash/uploadBegin/uploadChunk/uploadCommit/uploadAbort/mkdir/copy/move/zip/unzip/stat/setattr` | host-wide **absolute native paths, no editor_root confinement**; never rejects (`{ok:false,error}`); unknown host id fails closed | 86:235-387 |
| **Session RPC** | `session.procs(id)`, `session.kill(id, pid)` (destructive), `session.git(id)` | `{status, json}`, never rejects; 12 s default timeout | 86:462-514 |
| **Clipboard** | `clipboard.observe(fn)` | fn(dir, text) for every copy/paste core captures; auto-unsubscribed | 86:515-529 |
| **Taskbar** | `taskbar.addStatusItem(node)`, `onItemsRendered(cb)`, `interceptActivate(fn)` | status chip inserted before `#help-chip`; activation intercept receives a key | 86:530-550 |
| **Desktop hooks** | `desktop.columnFilter/onColumnCreated/onPlaced/onForgotten/onReveal/onLayoutRender` | **one slot per family**; second registration throws | 86:551-592 |
| **Keys/menus** | `registerKeyActions([...])`, `registerWindowMenuItems(fn)`, `registerDesktopMenuItems(fn)` | key actions merge into dispatcher + Shortcuts pane + help corpus | 86:593-615 |
| **Terminals** | `windows.onTerminalCreate(cb)` | replayed over open terminals; info bag: `{win, titleBar, host, wireId, addTitleBarItem, onDispose}` | 86:616-638 |
| **Settings** | `settings.boolean/radio/select/combo/text(key, …)` → `{get,set,onChange}` | synced /state blob + Control Panel control; invalid/duplicate options **throw and disable the mod**; `mount` targets applets (#181) | 86:639-678, 1232-1427 |
| **Theme** | `theme.get()` → `{name,dark}`, `theme.vars()` (10 public CSS vars, token streams), `theme.onChange(fn)` | fire-after-apply, no replay on register, re-entrancy bounded at 4 | 86:679-702, 792-1092 |
| **Panes** | `registerSettingsPane({id,title,mount,render,reflect})` | `render()` exactly once; `reflect` must be idempotent | 86:709-711, 1435-1480 |
| **Help** | `registerHelpCards(cards)` | typed span schema, sanitized, never raw HTML; plus per-mod `help.md` (#113) | 86:722-724, 1482-1572 |
| **Window kinds** | `registerWindowKind(spec)` | first-class app-window kind: factory, serialize, optional restore, `menu.iconKey`/`iconGlyph`; custom restore gets no dedup-by-id (#167) | 86:725-758 |

Explicit non-API (documented): no `ctx.registerAppIcon(svg)`; `--accent` and geometry vars excluded from `theme.vars()`.

### 3.2 Server-side facilities

| Facility | Route(s) | Auth | Notes |
|---|---|---|---|
| Installed-mod serving | `GET /mods/<id>/<gen>/<name>` | **PUBLIC, forced** | `<script src>` can't carry Authorization; content-addressed, immutable cache; source/CSS publicly readable; `gen` is an unsalted confirmation oracle (app.py:3147-3210) |
| Install/uninstall/rescan | `POST /mods/install\|uninstall\|rescan` | token, **not lease-gated** | validated fully in memory, atomic gen commit (app.py:6304+, modinstall.py) |
| Catalog | `GET /info` | token | `mods`, `mod_policy`, `update`, `restart`, `broker_id` (app.py:6119-6195) |
| Pins | `POST /mods/policy` | token, **not lease-gated** | PATCH `{"set":{id: bool\|null}}`, sidecar-persisted (app.py:6210-6253) |
| Mod KV | `GET/PUT /mod-store/<modId>` | token; PUT lease-gated | 50-deep revision ring, `purgeRevisions` (app.py:7462-7590) |
| Shared state | `GET/PUT /state` | token; PUT lease-gated | rev-based optimistic concurrency, 409 inlines live state (app.py:7374-7449) |
| Status egress | `GET /status/fetch` | **token only — no operator gate** | allowlisted Statuspage ids (structural SSRF defense) but any token holder triggers egress (app.py:6567-6607) |
| Update egress | `GET /update/check` | token + `update_check_enabled` (503 + zero egress off; gate re-read inside `update_lock`) | the model to copy (app.py:6609+) |
| Apply/restart | `POST /update/apply`, `POST /restart` | token + `apply_enabled`/`restart_enabled` | three-gate consent sidecar, config-presence wins, corrupt sidecar fails closed (app.py:429-548) |
| Recorder | `/recording/begin\|chunk\|commit\|abort`, `GET /recordings`, notes | token | server-generated `rec-*` ids are the whole traversal defense; 4 concurrent saves cap (app.py:7595-7900) |
| Help corpus | `GET /help-corpus.json` | PUBLIC, two bodies | no token → wiki + shipped only; token → plus installed sections (#173); gen oracle残 remains (app.py:3213-3278) |

Cross-cutting: single token predicate (`auth.request_token_ok`); single-active-client lease gates only `/state` PUT and `/mod-store` PUT — broker-config routes are deliberately lease-ungated; every sidecar mutation goes through shielded atomic writes.

---

## 4. What mods actually do outside the API

This is the evidence base for §5. The concatenated-script architecture makes core globals directly reachable, and the loader documents this as idiomatic (workspaces.js:30-35) — but the *patterns* below are where the missing surface shows.

### 4.1 The five strongest cross-mod signals

1. **App-window scaffold boilerplate (9 mods).** aistatus :264-355, clipboard :83-234, help :497-618, editor :2129-2159, file-manager :1651-1665, recorder :1051-1082 *and* :1661-1695 (twice), scratchpad :148-172 ("same scaffold as the clipboard / task-manager app windows"), task-manager :555-566, sticky :99-132, update :2096-2108. Each hand-builds a ~30-field `win` literal, calls `windows.set`, fabricates a synthetic `kind:'app'` session, appends a `buildTaskbarItem` chip to `#taskbar-items`, and removes `#taskbar-empty`. Unload iterates the core `windows` Map to close its own windows (aistatus :459-466); task-manager (:628-647) and update exploit LIFO teardown ordering so windows close while the kind is still registered — mods must know core's serializer-fallback internals.

2. **`.cap` capability stash for hoisted code.** ctx is init-scoped, but window builders are hoisted so core's restore fallback can call them. Result: editor :60-76 (`editorFile.cap` + fallback onto core-private `_modFileApi`), file-manager :60-155 (~95 duplicated lines mirroring the wire shape), task-manager :46-73 (`tmSession.cap`, `tmPausableInterval.cap`). termfont gives up and reads `getSettings().termFont` directly (:59) instead of its own ctx handle.

3. **`win._*` duck-typed convention hooks.** `win._onHostAuth` (help :605, editor :1564, file-manager :1673, recorder :1605/:2120), `win._hostRemoved` (file-manager :1673-1695), `win._saveToServer` for Ctrl+S dispatch (scratchpad :408), plus private repaint channels (`win._clipRender`, clipboard :190). Core calls these by convention; nothing registers them.

4. **Direct core-state reads/writes with no read API.** `windows`, `sessions`, `prefs` (help reads/writes `prefs._help` :52-85, load-bearing on two core internals), `frontId` — **written** by workspaces (:689/:717/:869), core privates `_stateReady` (help polls it 20×500 ms, :701-714), `_deactivated` (workspaces :259), `window.__mods.registered`/`settingToggles`/`helpCards` (mod-sync :162/:199, help :197), `modCatalogCache` read *and* `.delete()`d (update :887/:1307/:1843), loader privates `_pin`/`_modTextOk` via `typeof` (mod-sync :118-181).

5. **Cross-mod calls through hoisted globals.** pattern deliberately exports `applyPattern`/`PATTERNS` at top level relying on fragment parse order (pattern :12-24); theme probes `typeof applyPattern === 'function'` (:81-83); scratchpad declares `requires:['editor']` solely to share one CodeMirror build, then calls the editor's `loadCodeMirror()` global directly (:552).

### 4.2 Other notable reaches

- **xterm monkey-patching:** recorder replaces `term.write`/`term.resize` by assignment with restore-if-still-ours stack-awareness (:534-535, :636-641) and reads private `term._core._renderService.dimensions` (:1148, hardcoded 9×17 fallback). mousemode samples `term.modes.mouseTrackingMode` on `onWriteParsed` + rAF because there's no mode-change event (:20-34).
- **serverStore CAS reimplemented three times:** host-registry :895-908 (read-rev-then-set with one-shot 409 rebase), scratchpad :372-407 (single-in-flight + rebase + debounce + RO banner), mod-sync :459-506 (rewrites the `/state` wire entirely because `putHostState`'s 409 rebase re-PUTs a stale whole blob).
- **Raw authenticated HTTP:** update calls `hostFetch` for 5 routes (:610, :987, :1336, :1813, :1872); recorder likewise (:154, :238, :1539); mod-sync hand-rolls an AbortController because `hostFetch`'s deadline stops at headers (:485-495). recorder documents the `hostFetch(null, …)` silently-same-origin footgun (:105-111).
- **Host cache invalidation by hand:** host-registry's `invalidateHost` mutates **eight** core per-host caches, self-described as "a SUPERSET of what core's own edit path clears today — a known latent gap" (:795-804), and must hand-sequence five render calls to apply a change (:867-877).
- **Dialog/overlay gaps:** host-registry builds its own password inputs because `openDialog` fields are text-only (:1112-1127) and needs `_encBusy` single-flight because openDialog cancels the live dialog (:313-320); git hand-positions a popover against title-bar offsets with document-level capture listeners (:272-279); workspaces appends a preview popover to `document.body` with manual viewport clamping (:594-604).
- **Duplicated constants coupled to core (test-guarded):** termfont's `TERM_FONT_DEFAULT` must byte-match a literal in `67_js_window_lifecycle.js` (:40-46); theme's `night` palette must duplicate the `:root` CSS defaults (:38-42); file-manager's `FM_CHUNK_BYTES` must match server `MAX_CHUNK_BYTES` (:162).
- **`ctx.visibility` fallback boilerplate** repeated in aistatus :213-218, clock :80-85, git :57-64 — identical `{stop}`-shaped setInterval shims for older loaders.
- **Hand-rolled async cancellation:** update's `restartOpDead`/`applyOpDead` flags (:1255-1260, :2469-2470) because nothing aborts a mod's in-flight loops at teardown; git builds its own `disposers` Set + `decorated` WeakSet (:45-50) because `win.cleanups` only fires on window close, not mod disable.

---

## 5. Gap analysis

### 5.1 API completeness

#### G1. App-window factory — the #1 gap
- **Evidence:** §4.1 item 1 — nine mods, identical scaffold, all touching `windows.set`/`sessions.set`/`#desktop`/`#taskbar-items` directly.
- **Impact:** the single largest core-coupling surface; any change to the window record shape breaks nine mods at once, silently (hand-mirrored literals don't fail loudly).
- **Proposal (ctxVersion 2):**
  ```js
  const h = ctx.windows.createAppWindow({
    kind, id?, title, color?, geom?,
    singleton: true|false,            // formalizes clipboard's CLIP_WIN_ID open-or-focus hack (:34)
    body(el, win), toolbar?, onClose?, retainOnClose?
  });  // → {win, body, setTitle, setColor, close, focus}
  ctx.windows.closeAll();             // replaces the windows-Map iteration in unload
  ctx.windows.list();                 // read-only view of this mod's windows
  ```
  Chip, session entry, chrome, resize handles, placement, save — all core-owned. `registerWindowKind` keeps serialize/restore but its factory receives `createAppWindow` so restore paths share the scaffold.

#### G2. Events family — the biggest hole shaped like a family
- **Evidence:** one hack per missing event — `win._onHostAuth` (4 mods), `win._hostRemoved`, host-registry's 8-cache invalidation + 5-call render sequence, help's `_stateReady` poll loop, workspaces reading `_deactivated`, theme having to call pattern's `applyPattern` itself, git's hand-built disposer registry for mod-disable on live windows.
- **Impact:** every host-lifecycle and state-lifecycle interaction is an ad-hoc duck-typed protocol; core cannot refactor its auth form, host list, or boot sequencing without auditing every mod.
- **Proposal:** a uniform bus mirroring `ctx.theme.onChange`'s good semantics (fire-after-apply, documented replay, auto-unsubscribe on teardown):
  ```js
  ctx.events.on('host:auth' | 'host:changed' | 'host:removed' | 'state:adopted' | 'lease:changed', fn);
  ctx.hosts.invalidate(id);           // the core-owned invalidateHost host-registry wants
  info.onModTeardown(fn);             // per-window mod-scoped disposer in onTerminalCreate
  ```

#### G3. serverStore CAS/save-chain helper + a browser-local prefs tier
- **Evidence:** three independent CAS reimplementations (§4.2); help's `_help` underscore-prefs hack load-bearing on `resetLocalView` and `_stateBlob` internals (:52-85).
- **Impact:** conflict-handling bugs re-introduced per mod (mod-sync's motivation was exactly a stale-blob clobber, see gh158); no sanctioned tier between localStorage and the synced blob.
- **Proposal:** `ctx.serverStore.update(fn, opts)` (get → fn → set, auto-rebase on 409, bounded retries), `ctx.serverStore.saveChain(opts)` (scratchpad's debounced single-in-flight pipeline, canned), and `ctx.prefs.get/set` (browser-local, survives resetLocalView, never synced).

#### G4. Inter-mod services + commands
- **Evidence:** pattern→theme and scratchpad→editor via hoisted globals (§4.1 item 5); help's functions hoisted on purpose because "the mod system can't contribute keybinding targets" (help :30-77); scratchpad's Ctrl+S dispatched through `win._saveToServer` (:408).
- **Impact:** the docs admit editor/help/pattern "cannot be republished as installable packages unchanged" (7 pinned cross-fragment edges) — the portable-mod contract is blocked on this.
- **Proposal:** `ctx.provide(name, api)` / `ctx.consume(modId, name)` (auto-revoked on teardown, undefined when provider inactive — matches the feature-detect idiom), plus `ctx.commands.register(id, {run, when})` / `ctx.commands.execute(id, args)` so keybindings, menus, and core→mod dispatch target command ids instead of underscore fields.

#### G5. ctx.http and host identity
- **Evidence:** update and recorder use raw `hostFetch`; mod-sync rewrites the `/state` wire and hand-rolls timeouts; the `hostFetch(null, …)` same-origin footgun; update's `hostFingerprint` = host URL (:925-928) because there's no stable host identity; update reads/mutates `modCatalogCache` because there's no host-info/capability API.
- **Proposal:** `ctx.http.fetch(hostId, path, {method, json, timeoutMs /* total */, signal})` → `{status, json|text}`, never rejects, hostId **required** (null throws). `ctx.hosts.list()` with a stable fingerprint. Route-scoped families (like `ctx.session`) for update's routes, preserving the review-hygiene property that a mod's RPC surface is enumerable.

#### G6. Terminal access
- **Evidence:** recorder's monkey-patch + private-internals read; mousemode's sampling workaround; termfont's byte-matched constant.
- **Proposal:** extend the `onTerminalCreate` info bag — `info.tapOutput/tapInput/onResize` (composable, ordered, auto-removed — ends "restore only if the method is still our wrapper"), `info.cellDims()`, `info.setFont(family)` + a readable core font baseline.

#### G7. Dialogs, popovers, introspection, assets
- **Evidence:** §4.2 dialog/overlay gaps; mod-sync's DOM-scraping of `<option>` values (:125-133) and `window.__mods.*` reads; clock/update inline-`cssText` chips; editor's self-vendored module loader.
- **Proposal:** `ctx.dialog.open({fields:[{type:'password'|…}], queue:true})` with ownership token; `ctx.popover.anchor(node, anchorEl, {placement})`; read-only `ctx.mods.list()/isActive/pinOf`, `ctx.settings.describe(key)`, `ctx.helpCards.list()` (the `__test` API's shapes, promoted and frozen); `ctx.assets.url(name)` riding the existing `/mods/<id>/<gen>/` machinery.

### 5.2 Security / isolation / consent

**Framing (must survive into the next version's docs):** the browser page is the trust boundary. In-realm sandboxing is off the table — mods legitimately need `windows`, `sessions`, xterm internals, monkey-patching, and hoisted core-reachable builders; ctx coverage ≈ 0% of actual reach, so a runtime capability gate on ctx would be theater. Spend the budget on the network edge and the supply chain.

#### G8. `/status/fetch` has no operator gate — confirmed hole
- **Evidence:** broker inventory §4; app.py:6567-6607 — allowlisted provider ids (good SSRF defense) but token-only; the aistatus mod being default-off is cosmetic as egress protection (established in gh182).
- **Impact:** any token holder (any tab, any mod) triggers broker egress with zero operator consent.
- **Proposal:** clone the #182 pattern exactly — `status_fetch_enabled`, 503 + zero egress when off, gate re-read inside a lock. Low cost; tested pattern exists.

#### G9. Token-gated (not lease-gated) install/policy collapses per-mod review
- **Evidence:** `POST /mods/install` and `POST /mods/policy` are token-gated only (broker inventory §1-2/§7). Any running mod holds the page token → **any mod can install another mod**, or pin mods fleet-visibly. Composition risk: mod-sync administers remote brokers entirely outside ctx (its only ctx call is `registerSettingsPane` at :1226) using tokens from host-registry's `/mod-store` blob — whose 50-deep revision ring **retains the plaintext you just encrypted** (gh175). Compromise one page → fleet.
- **Impact:** "review one mod, get all future mods."
- **Proposal:** an admin-token class (or `remote_writable`-style capability, already invented for #182) for `/mods/install|uninstall|policy` and `/update/policy`; a dedicated secret store (or mandatory `purgeRevisions` + encrypt-at-rest) for credential blobs the ring never touches. Medium cost: mod-sync and update depend on today's openness and must be migrated.

#### G10. Browser-side egress ungoverned
- **Evidence:** no CSP constrains `fetch`/XHR/off-origin `import()`; gh165 confirmed the CSP genuinely leaves gaps (no `default-src` fallback). A malicious installed mod could exfiltrate the token, the clipboard stream (`ctx.clipboard.observe` sees every copy/paste), or `ctx.file` reads.
- **Impact/leverage:** the audit shows **all current mod network traffic is same-origin or hostFetch to registered brokers** — so a tight CSP (`default-src 'self'`; `connect-src 'self'` + registered hosts; `frame-src` for the gh165 iframe gap) breaks nothing and is the **single highest-leverage browser-side control**.
- **Proposal:** dynamic CSP header enumerating registered hosts. Low-medium cost.

#### G11. Capability declaration as review tooling, not runtime gate
- **Evidence:** `tiers` is declared-not-enforced (86:143-152) — worse than nothing, it invites reviewers to trust the label (mod-sync declares `['settings']` while administering other brokers). No signing anywhere; `x-<author>-<name>` scoping has zero validation; installed-but-disabled is not containment (top-level code runs regardless; Writing-a-Mod §10.5).
- **Proposal:** (a) a declared-capabilities manifest (`needs: ['file','session','egress','clipboard','remote-admin']`) surfaced at install/review time and **statically linted** at install validation (the portable-mod lint already does source-text checks in `modinstall.py`'s orbit) — make `tiers` real as a lint, never pretend it's a runtime boundary; (b) package signing — author keypair, signature in the manifest, broker verifies against a pinned key before install. Signing is the only thing that makes fleet-wide/sync-driven install defensible.

**Explicitly reject:** iframe/worker isolation; runtime ctx capability gates; per-mod `defaultEnabled` as a security control.

### 5.3 Lifecycle / DX

#### G12. The shared-scope module format is the root cause
- **Evidence:** one concatenated `<script>` (86:11-14) causes: no live install/reload ("Identifier already declared", 86b:10-19 — forced, not unimplemented); the TDZ landmine class (86:16-21; workspaces' `atInit` dodge :132-147; host-registry's `_encSetting` pre-seed :322-326; #162's whole-mod-disabled failure); the 2500-line fragment cap (update.js 2496/2500, companion files shaped by the node test harness's purity constraint, update-policy.js:16-19); the `.cap` stash; hoisted-global cross-mod coupling; namespace collisions by luck (editor's `typeof openAgentDocsWindow` guard for a *retired* mod, :109-119).
- **Proposal:** **per-mod ES modules.** Each mod becomes `export default {id, init, …}` loaded by the loader; core exposes its API surface via an import. Installed mods already ship as separate content-addressed SRI'd URLs — 90% of the way there; the editor already `import()`s vendored `.mjs` (codemirror.js :132). This one change fixes collisions, TDZ blast radius, the cap, `.cap`, and cross-mod imports, and unlocks **hot reload**: `_takeDown(id)` (already reverses everything) + `import('/mods/<id>/<newGen>/entry.mjs')` — a new gen is a different URL, so the module cache never collides. Prerequisites: per-gen stylesheet tracking/removal (currently impossible), and `ctx.signal` (below). Per AGENTS.md this is the change that needs the codex adversarial pass; the hard cases are the deliberately-hoisted core-reachable mods (editor's restore fallback, help's keybinding targets), which need G1/G4's registration seams first.

#### G13. Async lifecycle, error softening, missing timer guarantee
- **Evidence:** update's `*OpDead` flags; git's disposer registry; a duplicate settings option **nukes the whole mod** (clock dedups defensively :130-134); async validators' truthy Promise accepts everything (#168); the `ctx.visibility` fallback triplet.
- **Proposal:** `ctx.signal` — a per-mod AbortSignal fired at teardown (kills hand-rolled cancellation and stray timers/fetches); settings-option validation errors downgraded to `{ok:false}` warnings with a status row instead of mod-fatal throws; await-or-reject-loudly for Promise-returning validators; guarantee `ctx.visibility` at the platform floor so the fallbacks die.

#### G14. Versioning is a single frozen integer
- **Evidence:** `ctxVersion: 1`, everything since additive; mods sniff loader *privates* by name (`typeof _pin`, `typeof _modTextOk`); feature-detecting mods degrade silently (git is inert without `ctx.windows`, no operator-visible reason); mod `version` is display-only; no migration hooks; no update channel for installed mods.
- **Proposal:** per-capability version map `ctx.capabilities = {file: 3, serverStore: 2, …}` + declarative `needs: ['windows.onTerminalCreate']` in `registerMod` → mod surfaces as `blocked (needs windows)` in the Mods pane (the status vocabulary at 86b:772-795 already has the slots); semver `version` + `migrate(fromVersion, storage)` hook run once per upgrade; an installed-mod update check riding the `/update/check` gate model.

#### G15. No testing story; docs drift structurally guaranteed
- **Evidence:** "CI never executes UI JavaScript"; `__test` bypasses `setModEnabled` and is a fixture, not an SDK; the test harness — not the API — shapes update's file layout; Writing-a-Mod carries a standing staleness header and §8 is hand-maintained prose.
- **Proposal:** ship a node-loadable `mock-ctx.mjs` over in-memory fakes (ESM makes all mod code importable); a dev-mode loader flag (`?moddev=<url>`) importing an entry from localhost with cache-busting (~30 lines under ESM); a documented Playwright recipe (`?nomods=1` + `__test.run` are already the ingredients); generate the ctx API table from JSDoc on `makeCtx` behind the same byte-exact regen gate the help corpus already uses.

---

## 6. Recommended next-version mod platform

### 6.1 Prioritized plan

**MUST (highest evidence weight; mostly pre-ESM, deliverable incrementally on ctx v1 as additive):**

| # | Item | Gap | Why first |
|---|---|---|---|
| M1 | `ctx.windows.createAppWindow` + `closeAll`/`list` | G1 | 9 mods, largest coupling surface; shrinks editor/file-manager/update below cap pressure |
| M2 | `ctx.events` (host:auth/changed/removed, state:adopted, lease:changed) + `ctx.hosts.invalidate` + `info.onModTeardown` | G2 | kills every `win._*` protocol and the `_stateReady`/`_deactivated` reads |
| M3 | `ctx.serverStore.update` + `saveChain`; `ctx.prefs` | G3 | three reimplementations already; host-registry stores *credentials* through hand-rolled CAS |
| M4 | Server: `status_fetch_enabled` gate; CSP (`default-src 'self'`, dynamic `connect-src`, `frame-src`) | G8, G10 | the two cheap, non-breaking, high-leverage security wins |
| M5 | Admin-token class for `/mods/install\|uninstall\|policy`, `/update/policy`; secret-store fix for the `/mod-store` plaintext ring | G9 | closes "any mod installs any mod" and the crown-jewel token exposure |
| M6 | Capability map `ctx.capabilities` + declarative `needs` | G14 | cheap; every later addition benefits; ends loader-private sniffing |

**SHOULD (platform v2 proper):**

| # | Item | Gap |
|---|---|---|
| S1 | ES-module-per-mod format + hot reload (dispose-then-reimport of content-addressed gens); prerequisites: per-gen stylesheet removal, `ctx.signal` | G12, G13 |
| S2 | `ctx.provide/consume` + `ctx.commands` | G4 — unblocks the portable-mod contract for editor/help/pattern |
| S3 | Terminal taps (`info.tapOutput/tapInput/onResize`, `cellDims`, `setFont`) | G6 |
| S4 | `ctx.http.fetch(hostId, …)` + `ctx.hosts.list()` with stable fingerprint; route-scoped `ctx.update.*` | G5 |
| S5 | Install-time capability lint (make `tiers`/`needs` checkable) + package signing | G11 |
| S6 | Error softening: settings-option errors non-fatal; async validator handling; `mock-ctx.mjs` + `?moddev=` dev loop | G13, G15 |

**COULD (defer, but name in docs so silence isn't ambiguity):**

- `ctx.dialog` password/queue fields, `ctx.popover.anchor` (G7)
- Introspection getters (`ctx.mods.list`, `ctx.settings.describe`, `ctx.helpCards.list`) (G7)
- `ctx.assets.url`, blessed `addStatusItem` style hook (kills inline `cssText` chips)
- `migrate(fromVersion, storage)` hooks; installed-mod update channel (G14)
- Doc generation from `makeCtx` JSDoc with a drift gate (G15)
- i18n — document as an explicit non-goal for now; the typed help-card schema is the natural later entry point

### 6.2 Migration notes for the 19 existing mods

| Mod | Primary migrations | Effort |
|---|---|---|
| **aistatus** | M1 window factory replaces :264-355; drop visibility fallback (:213-218); `ctx.windows.closeAll()` replaces :459-466; route poll via S4 or keep `/status/fetch` (now behind M4 gate — operator must enable) | low-med |
| **clipboard** | M1 with `singleton:true` replaces the `CLIP_WIN_ID` hack (:34) and scaffold (:83-234); needs one new bit — observer echo suppression (`observe` should mark mod-initiated copies) — or keep `_selfCopy` | low |
| **clock** | Drop visibility fallback; option-dedup becomes unnecessary under S6; optionally move inline chip styles to `ctx.assets` CSS | trivial |
| **editor** | Hardest. `.cap`/`_modFileApi` dies when restore factories receive ctx (M1/G-A2 seam); `win._onHostAuth`→M2 events; `openAgentDocsWindow` guard deletable; CodeMirror vendoring becomes `ctx.provide('codemirror')` (S2) for scratchpad; ESM migration blocked until the restore-fallback seam exists | high |
| **file-manager** | Same `.cap`/`_modFileApi` removal; `win._hostRemoved`/`_onHostAuth`→M2; `FM_CHUNK_BYTES` should be served by the API; window scaffold→M1 | med-high |
| **git** | `info.onModTeardown` (M2) replaces the disposer Set/WeakSet (:45-50); popover→`ctx.popover` (could); drop visibility fallback | low |
| **help** | Hoisted-on-purpose functions become `ctx.commands` targets (S2) — that is the whole reason they're hoisted; `_stateReady` poll→`state:adopted` event; `_help` prefs key→`ctx.prefs`; `window.__mods.helpCards`→introspection getter | medium |
| **host-registry** | 8-cache `invalidateHost`→`ctx.hosts.invalidate` (M2); 5-call render sequence→`host:changed`; CAS→`serverStore.update` (M3); **token storage moves to the M5 secret store** (this is the priority migration); password dialogs→could-tier dialog API | medium |
| **mod-sync** | Most affected by M5: needs the admin-token class to keep administering peers; loader-private sniffing (`_pin`, `_modTextOk`)→`ctx.capabilities`+introspection; `/state` wire rewrite→a per-key merge API (worth folding into M3's design); DOM option-scraping→`ctx.settings.describe` | high |
| **mousemode** | Already ctx-clean; `tapOutput`/mode events (S3) would remove the `onWriteParsed` sampling, else no change | trivial |
| **pattern** | `applyPattern` global→`ctx.provide('pattern', {apply})`; theme consumes it; alternatively pattern subscribes to `ctx.theme.onChange` and the coupling disappears entirely | low |
| **recorder** | Monkey-patches→`info.tapOutput/tapInput/onResize` (S3); `_core._renderService.dimensions`→`cellDims()`; window scaffold ×2→M1; `win._onHostAuth`→M2; raw `hostFetch`→S4 or a `ctx.recording.*` family mirroring `ctx.session` | med-high |
| **scratchpad** | Save pipeline→`serverStore.saveChain` (M3, it *is* the reference implementation); `loadCodeMirror()`→`ctx.consume('editor','codemirror')`; `win._saveToServer`→window-scoped command (S2); scaffold→M1 | medium |
| **sticky** | Taskbar chip recipe→M1 (or a `ctx.taskbar.addWindowChip`); the init-pass race (:150-168) wants an app-window replay hook analogous to `onTerminalCreate` — fold into M1's design | low-med |
| **task-manager** | `.cap` stash dies with M1 (factory receives ctx); `_modSessionApi` fallback becomes unnecessary; teardown-ordering hack (:628-647) dies with `closeAll()` | low-med |
| **termfont** | `TERM_FONT_DEFAULT` duplication→`ctx.terminals.defaults.fontFamily` (S3); hoisted `getSettings()` read fixed by module scope under S1 | trivial-low |
| **theme** | `applyPattern` probe→S2 or event-driven pattern; `night`-palette duplication wants a readable core default (fold into theme API); add teardown (currently none reverts CSS vars) | low |
| **update** | Biggest RPC consumer: 5 raw routes→S4 route-scoped family; `modCatalogCache` reach→a host-capability API; `*OpDead`→`ctx.signal`; scaffold→M1; **M1 alone likely frees enough lines to end the 2496/2500 cap crisis**; under M5 it needs the admin token for policy writes | high |
| **workspaces** | Already the model ctx consumer. Remaining: `frontId` writes want a `ctx.focus` API; `_deactivated`→`lease:changed` (M2); chip decoration wants a blessed decoration hook (could); `prefs._floatWs`→`ctx.prefs`; re-registrable unload would fix the one-pager-node-for-life pattern | medium |

### 6.3 Sequencing

1. **Now (ctx v1 additive):** M1–M6. M1+M2+M3 delete the boilerplate that makes every mod fragile and shrink the files under cap pressure. M4+M5 are independent server work.
2. **Then:** migrate mods onto the new surfaces (the table above), which is exactly the prep that makes editor/help/pattern portable.
3. **Then:** S1 (ESM + hot reload) as the major-version structural change — run the AGENTS.md codex adversarial pass on it first; the known hard cases (editor restore fallback, help keybinding targets) are already de-risked by S2/M1.
4. **Throughout:** S5 signing/lint before any sync-driven or fleet-wide install feature is even contemplated; S6 testing/dev-loop as soon as ESM lands (it's what makes mock-ctx cheap).

The one-sentence thesis for the next version: **keep the honest trust model, move the security work to the network and supply-chain edges where it can actually hold, and pay down the shared-scope architecture — first with APIs that delete the nine-fold boilerplate, then with a module format that makes mods real units of code instead of regions of one script.**