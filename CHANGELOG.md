# Changelog

Notable changes from 0.8.0 onward are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file starts at 0.8.0: the most recent development cycle is itemized, and
everything before it gets a coarse summary rather than an entry-by-entry
reconstruction. **Detailed history before 0.8.0 lives in the git log and the
[issue tracker](https://github.com/JohnConnorNPC/browserland/issues)** — most
changes land with their issue number in the commit subject, so
`git log --oneline --grep '(#153)'` is usually the fastest way to read the real
story behind anything named below.

## [Unreleased]

### Added

- **`wiki/` is now the single source for every piece of prose in the project.**
  `docs/` — 2,528 lines of developer and operator reference — never rendered in
  the app, was never searchable, and drifted on its own. It has been merged into
  `wiki/`, which was already the single source for the in-app Help window, and
  deleted. Every reference in the repo points at the new paths.
- **Developer pages are reachable from inside the desktop**, behind an
  **Include developer docs** checkbox in the Help window, so the default view
  stays the end-user guide. A page declares its audience with
  `<!-- help:tier dev -->` front matter; the parser is strict about it, because a
  typo that silently published internals into the end-user guide is the failure
  that matters. The choice is per browser and survives *Reset local view*.
- **Fenced code renders as real code.** The corpus parser folded a fenced block
  into one space-joined line, which no multi-line `curl`, systemd unit or JSON
  sample survives. Blocks now keep their lines and their indentation, and the
  Help window renders them in a scrollable `<pre>`.
- **New pages** for two features that were documented in neither surface:
  **Installing Mods** (install/uninstall, the shipped-vs-installed badge, the
  install preview, that installs apply on the *next page load*, and the
  `?nomods=1` rescue hatch) and **Themes & Appearance** (the five colour schemes,
  the background patterns, the opt-in terminal font). The `theme`, `pattern` and
  `termfont` mods ship a `help.md` for the first time.
- **Drift guards** (`tests/test_wiki_drift.py`): the default-keybinding table is
  checked against the real defaults, no file may reference the retired `docs/`
  tree, and every shipped mod must carry a `help.md` — so a new feature arrives
  with documentation instead of a year later.
- **A workflow publishes `wiki/` to the GitHub Wiki** on push to `main`, gated on
  the packaged corpus being current.

### Fixed

- **The wiki now describes what ships.** An accuracy pass corrected roughly
  twenty claims, among them: the (+) menu's order (it is window-kind
  registration order) and its labels (SVG icons, not emoji); `Undo` being the
  last item of the desktop menu rather than an arrangement action; Pull merging
  every configured broker with a "sources disagree" flag; the Mods pane having
  gained install/uninstall; the mouse-mode chip shipping **on**, not off;
  rebinding using explicit Set/Clear buttons; OSC 52's rate limit; and
  `read_screen`'s wait/scrollback/attrs options.
- **A `##` heading inside a fenced code block no longer splits a page in two.**
  No page had one, but 2,528 lines of developer prose is exactly where the first
  would appear. An unclosed fence is now a build error rather than silent
  mangling.
- **Emphasis containing inline code renders as emphasis.** `**a `b` c**` put its
  `**` markers on screen as literal punctuation, because the parser split on code
  spans before it looked for emphasis.

- **Pull can read the broker list from any broker you have configured** (#174).
  Publish has been multi-broker since #65; Pull could only read the broker whose
  page was open, which made recovering a list published to broker B mean loading
  B's page first — circular when B was the very host missing from the list. With
  more than one broker configured, **Pull…** now asks which brokers' lists to
  read: this one is ticked by default, each other one is listed with what it
  holds ("3 hosts", "passwords encrypted") or why it can't be read ("refused our
  password", "could not be reached", "nothing published there"), and unreadable
  ones are shown but not selectable. With only one broker configured the picker
  is skipped entirely, so nothing changes for that case. Opening it forces no
  password prompt, and a broker with no saved password is not contacted at all.
  Reading several brokers merges them into one set of rows, de-duped by address
  with the first source winning and every row saying which broker it came from
  and which ones disagreed. Two *different* addresses claiming to be the same
  broker are kept as two flagged rows rather than silently merged — broker ids
  in a registry are unverified, so treating one as an identity would let a list
  swallow another's entry by claiming it.
  Reading someone else's list is a new direction of trust, so three things are
  never taken from a remote one: **passwords** (unless you tick *Accept
  passwords from other brokers*, and you still tick each host by hand),
  **loopback addresses** (they name the publisher's machine, and importing one
  here would point a host at *your* broker carrying somebody else's password),
  and **hidden**, which takes a host out of the list you can see while this
  browser keeps talking to it.
  Two related fixes fell out of it. An entry's `id` is no longer a match
  candidate: it belongs to the browser that published the list, and since your
  own ids appear in every list you publish, a copy somebody else controlled
  could name one and repoint the host it belonged to. And an apply no longer
  writes an imported `broker_id` into your prefs — it clears it, so the identity
  is re-learned from the new address's own `/info`.
  A list carrying a *different password* for a host you otherwise already have
  now classifies as **differs** instead of "already have". It was greyed out
  before, so a rotated password could never be pulled back at all.

- **The Broker registry can encrypt what it publishes, in the browser** (#175).
  The registry was stored as plain JSON on the broker (`webterm_modstore.json`
  plus a revision ring), so **Include passwords** meant writing every broker
  token you published to that machine's disk in the clear. **Control Panel →
  Browser → Broker registry encryption** now offers three modes: *passwords
  only* (the default), *whole list*, and *off* (the old behaviour). WebCrypto
  only — PBKDF2-SHA-256 at 600 000 iterations with a fresh random salt per
  publish, then AES-GCM-256 with a fresh IV and the envelope header bound as
  additional authenticated data. The passphrase is held in memory for the page's
  life so publish-to-all and back-to-back pulls ask once, and is never persisted;
  there is no "remember on this browser", and **Forget passphrase** drops it.
  Each password is sealed *together with* the address it belongs to, and an
  unlocked pull uses the decrypted list alone — so nobody who can write the
  registry can edit the readable half to point one of your saved passwords at a
  machine of theirs. If the two halves disagree you are told.
  The default costs an existing user nothing: with **Include passwords** off,
  *passwords only* has no secret to encrypt, so the published value is
  byte-identical to before and no passphrase is asked for.
  Two behaviour changes worth knowing. `crypto.subtle` is secure-context only
  and the broker terminates no TLS, so on `http://<lan-ip>:4445` encryption is
  unavailable — and publishing is **refused** rather than quietly falling back
  to the clear, so publishing passwords from such a page now needs the mode set
  to *off* first. And an encrypted publish clears the broker's revision ring,
  because the plaintext value it replaces would otherwise stay readable in the
  history of the store you had just stopped trusting.
  **This is not protection from the broker itself** — that broker serves the
  code that does the encrypting. It protects the registry at rest, and from
  anyone who can read the store: the file, its backups, the revision ring,
  another admin of the machine, or a browser holding the broker token but not
  the passphrase. `host-registry/help.md` says so in as many words.
  **Forget passwords** still works with no passphrase — that is the emergency
  path. An encrypted block is opaque, so it is assumed to hold passwords and
  removed whole; under *whole list* that means the list goes with them, and the
  confirmation says so before you agree.

- **The taskbar's broker status chip can be turned down or off** (#178).
  **Control Panel → Broker status chip** takes **Always** (the default, and
  unchanged from before), **Only when a broker needs attention** — drawn only
  while some broker is unreachable, wants a password, or is held by another
  browser, and gone again once nothing is wrong — or **Never**. The chip used to
  render unconditionally because it was the only home for the per-broker hide
  toggle; #149 gave every broker a live row in the start (+) menu carrying that
  toggle plus log-in and take-over, and a **hidden** checkbox under
  Control Panel → Hosts, so the chip is now a pure indicator and nothing becomes
  unreachable without it. Attention mode inherits the existing two-failure
  debounce, so a single dropped poll never flickers the chip into view, and a
  broker you hid yourself never triggers it. The setting follows your browser
  rather than a host tab, and syncs to other browsers viewing the same broker.

### Removed

- **The Agent-docs mod is retired** (#177). Each terminal title bar carried a
  📋 button that opened that folder's `AGENTS.md` and `CLAUDE.md`. It had no way
  to know which folder you meant: it used the terminal's *detected* working
  directory, which is inferred by walking the OS process tree rather than
  reported by the shell, so it can lag a `cd`, prefer a lingering agent's
  directory over the shell's, fall back to a known-wrong parent for an
  unrecognised tool, or go stale entirely when the process is unreadable. When
  the guess was wrong the button opened a *different project's* `AGENTS.md`,
  silently, with the same window it shows when it is right — and because that
  window saves, a wrong guess did not just show the wrong file, it wrote to it.
  Nothing else is lost: `AGENTS.md` and `CLAUDE.md` are ordinary files that the
  text editor and file manager still open. Agent-docs windows you already have
  keep working — they restore, tab, and save exactly as before; only the ability
  to open a *new* one from a terminal is gone. A desktop that is already open
  keeps its 📋 buttons until the page is reloaded, as every mod change does.
  The same inferred directory still
  feeds the git-status widget, the file manager's start folder and the task
  manager, where being wrong is cosmetic or obvious rather than destructive.
  The mod is not deleted. It moves verbatim to a new
  `webterm/broker/mods-deprecated/` tree whose README says what would have to
  change before it is worth bringing back — the working directory being
  *reported* (shell integration / OSC 7) instead of inferred — and how to copy
  it back. That tree is the reusable mechanism for future retirements; it is
  repo-only and deliberately not shipped in the wheel.

### Fixed

- **Brokers you hid on purpose no longer count as needing attention** (#178).
  The aggregate badge computed both its `K need attention` count and its colour
  over every host with no regard for whether you had hidden it, so hiding an
  offline broker — the documented way to park one — left the badge reading
  `3 brokers · 1 needs attention` in red for good. Hidden is a chosen state, not
  a fault: it is excluded from the count and from the colour, and an all-hidden
  badge now drops its state colour entirely instead of claiming to be healthy.
  The badge stops *claiming* a fault but never stops *reporting* one — its hover
  list and the (+) menu rows still show every broker's real state, marked
  `— hidden`. The same fix stops a freshly added broker counting as down for one
  tick before its first poll has even been attempted.

- **A window drag or resize no longer stalls over embedded content** (#176).
  Both gestures tracked on bare `document` mousemove/mouseup, so anything that
  swallows events — an `<iframe>`, whose events belong to its own document, or
  a canvas that stops propagation — starved the listeners: the window stuck at
  the last position `document` saw, the release never arrived, and the gesture
  stayed live afterwards (a later buttonless mouse move kept resizing it).
  Two things fix it, and both are needed. The gesture raises a transparent
  full-viewport shield, so nothing underneath is hit-tested at all; and it takes
  pointer capture, which gives clean routing within the page plus a
  `pointercancel` / lost-capture / blur lifecycle that ends the gesture cleanly.
  Capture alone was not enough: a **cross-site** iframe runs in a separate
  browser process and is hit-tested before the capture is consulted, so the page
  saw none of the gesture even while it held the capture — measured at 0 of 16
  moves in Chrome 150. A move that arrives with no mouse button down now also
  ends the gesture, so a release the browser never delivered can no longer
  strand it.
  Not covered: a gesture that starts on a **tiled** window's title bar (the
  tiling strip runs its own drag engine and takes neither), and any content the
  browser paints in its *top layer* — a native `<dialog>` or popover — which
  sits above the shield. Nothing in the app uses either today.

- **Double-click stopped working page-wide for 700 ms after any window drag or
  resize** (#176). The guard that suppresses the stray click a finished gesture
  leaves behind was installed on `window` with no target check, so for 700 ms
  after moving essentially any window, every double-click died: opening a file
  or entering a directory in a file-manager or file-picker pane, renaming a
  scratchpad tab, and renaming another window by its title — the very thing the
  guard existed to protect. It is now scoped to the title bar the gesture
  started from, and the resize path, which never needed it, no longer arms it.

- **Touch could no longer pan the tiling workspace from a tiled window's title
  bar** (#176). `touch-action: none` was applied to every title bar, including
  tiled ones, which live inside the horizontally scrolling strip. Floating
  windows keep it (their drag is a captured-pointer gesture that a pan claim
  would cancel); tiled ones no longer do.

## [0.8.0] - 2026-07-30

The version number catches up with what has actually shipped: `0.1.0` was the
number the project was born with and had stopped describing it. No breaking
changes — see [`wiki/Upgrading.md`](wiki/Upgrading.md) for the record of those.

### Added

- **Runtime mod install** (#163). A broker can install, uninstall and rescan mod
  packages while it is running — previously a mod had to be listed in `ui.py`'s
  `_MODS` and baked into the page at import. Five slices: the on-disk store,
  scanner and `/mods/<id>/<gen>/<file>` asset route; the install/uninstall/rescan
  API; the loader that topologically sorts installed packages and late-registers
  them; installed mods' `help.md` merged into the Help corpus at serve time; and
  the Control Panel Mods pane with Install/Uninstall and a provenance badge that
  renders a peer's claim as a claim.
- **The `x-` mod-id namespace is reserved for installed mods** (#172). One mod id
  keys five namespaces (pins, per-browser overrides, stored data, assets, help),
  so an unprefixed third-party id could inherit a first-party mod's state.
- **[`wiki/Writing-a-Mod.md`](wiki/Writing-a-Mod.md), the mod-authoring guide** (#171) — the `ctx`
  contract used to live only in `86_js_mod_loader.js` comments. The portable-mod
  rules it states are now linted over `mods/**/*.js` by the test suite.
- **`ctx.theme`** (#169): read the live theme and subscribe to changes, derived
  from the live DOM so it is correct with the theme mod disabled.
- **`ctx.settings.text`** (#168), the free-text settings primitive, with an
  optional suggestion list. The clock's time-zone box now accepts any zone the
  browser's engine knows — as a fixed `select`, `clockTz` silently lost zones
  across engines.
- **Text glyphs for mod-owned app icons** (#170). `APP_ICON_SVG` was a closed
  table, so a mod-owned window kind could not carry its own icon.
- **Semantic `--ok` / `--warn` / `--danger` theme vars** (#173), replacing a
  status palette copy-pasted across core and four mods.
- **Hide the workspace pager** (#162) — a Control Panel option; mod-contributed
  desktop-menu items are now offered in floating mode too.
- **The running build id is baked into the served page** (#22):
  `<meta name="webterm-build" content="0.8.0+<sha>">` in `<head>`. A broker
  serves the page it assembled at import, so `git pull` changes nothing until
  the process restarts; `curl -s <host>/ | grep webterm-build` against
  `git rev-parse --short HEAD` in that broker's checkout is now the check (a
  wheel install has no git and reports the bare version). It is a meta tag
  rather than a value in the inline script because the CSP `sha256` covers that
  script's exact bytes.

### Changed

- Version bumped `0.1.0` → `0.8.0`, and the version now has a **single source**:
  `webterm.__version__`. `pyproject.toml` declares `dynamic = ["version"]` and
  reads that attribute, instead of carrying a second copy with nothing keeping
  the pair in sync.

### Fixed

- **App windows no longer vanish on reload** (#167). Window restore raced the mod
  loader, so a file-manager or scratchpad window could be silently dropped;
  restore now retries, and enabling a mod mid-session restores its windows too.
- A workspace hover preview no longer strands when the pager dots rebuild.
- Adopting a mod setup from a peer now says when a mod is not installed on the
  broker being adopted from, instead of appearing to succeed (a follow-up to the
  cross-broker mod sync in #158).

### Security

Neither of these is an advisory. The first is an information-disclosure
finding, introduced and fixed within this release; the second is a design
property you have to understand before you install a mod.

- **`GET /help-corpus.json` became an enumeration surface for installed mods.**
  The route is deliberately public (the login page renders its own Help window),
  and since runtime mod install (#163) its body also carried every installed
  mod's help section — which made the ids of installed mods that ship help,
  their help text, and their manifest's label and icon readable by anyone who
  can reach the port. What leaks is that help material and the mod ids around
  it: no token, and no file the mod did not already publish as documentation.
  **Fixed before release.** The route now answers with two bodies off the one
  URL — without a token, the wiki + shipped-mod corpus, byte-identical to what
  it served before runtime mod install; with a valid token, that plus every
  installed mod's help section. It stays public rather than becoming a `401`
  because the login page renders its own Help window, and `Cache-Control:
  no-store` + `Vary: Authorization` keep a compliant cache from handing one
  audience the other's body. The `/mods/<id>/<gen>/<file>` asset route stays
  public by design, and since `<gen>` is a content hash of the package rather
  than a secret, anyone holding a distributed package's bytes can still confirm
  that specific mod is installed — a confirmation oracle over a candidate set
  the caller already has, not enumeration.
- **Installing a mod is the decision to run it; disabling it is not
  containment.** An installed package's scripts are fetched and executed on
  every page load whatever its enabled state, and its stylesheets are live
  either way — `off` only means the mod's `init()` is not called. Installed mods
  are always reported `default_enabled: false`, which is an install/enable
  split, not a sandbox. Mods run same-origin with the broker's full authority;
  see [`wiki/Writing-a-Mod.md`](wiki/Writing-a-Mod.md) (#163, #171).

### Earlier history (summary)

Everything below landed before the cycle above and is summarized on purpose —
an honest coarse list beats a fabricated detailed one. Each item names issues to
read for the real story; those lists are entry points, not complete sets.

- **Mod system** (#71, #74–#86, then extractions and follow-ups including #106,
  #112, #113, #116, #120, #121, #124, #126).
  The frontend extension API the desktop is now built on: the `ctx` surface, mod
  CSS routed into `<head>`, window kinds, the `requires` dependency primitive,
  per-mod `help.md`, and the extraction of
  Help, theme, pattern, clock, sticky notes, the text editor, file manager, task
  manager, clipboard, scratchpad, git status, terminal font, AI status and
  agent-docs into mods.
- **Session recorder** (#140, #145, #151, #159, #161): per-terminal capture with
  a player, auto-record and size-cap rolling, gzip on disk with a plain JSONL
  wire, a library that reaches every connected broker, and an explicit warning
  that recordings can contain secrets.
- **Per-broker mod policy and cross-broker sync** (#157, #158): view and edit a
  remote broker's mod pins from its Control Panel tab, and push or adopt a whole
  mod setup between brokers.
- **Workspaces became a mod** (#147, #148, #152): the tiling core went
  single-desktop and the mod owns the workspace model through
  `ctx.desktop.columnFilter`, so disabling it is non-destructive.
- **OSC 52 clipboard bridge** (#153) — "copy" inside a TUI reaches the browser
  clipboard, default-off per host.
- **No third-party origin executes in the token's origin** (#143, #146): xterm
  and then CodeMirror were vendored and are served by the broker, and
  `script-src` is `'self'` plus a `sha256` of the page's one inline script — no
  `'unsafe-inline'`, no CDN.
- **Auth** (#142 **breaking**, #144): a token is required on every surface and
  every interface including loopback — the exceptions are the ones login has to
  bootstrap through, `GET /` and the base help corpus — and it no longer rides
  the query string where Resource Timing, HAR exports and proxy logs can read it.
- **Multi-host federation** (#24, #64, #65, #149): an optional broker-stored host
  list, the aggregate status badge and per-broker rows in the start menu,
  broker-id-gated terminate, and one MCP server across N brokers.
- **MCP surface** (#15, #21, #23, #26, #33, #52, #127–#136): rendered screen grids
  instead of raw ANSI, screen-vs-scrollback, delta and wait-for-content reads,
  styled `attr_runs`, cursor-masked stable hashes, key pacing and input flush,
  DECCKM-aware arrows, and the pyte-absence signal.
- **File manager transfers** (#105, #108–#111): chunked cross-host streaming past the
  old 5 MiB cap, a progress window with Cancel, and checksum-verified move.
- **Mouse modes** (#154, #155): tracking re-asserted after a reload, plus the
  ambient title-bar chip and the documented escape gesture.
- **Broker robustness** (#87, #150, #160): headless mode that serves the JSON/WS
  API with no page, a non-blocking check for an unavailable server, and
  `asyncio.shield` around write regions so a cancelled request cannot release a
  lock while its worker is still writing.
- **The served page stopped being a monolith** (#60, #68): one ~16.8k-line
  `index.html` became ordered fragments that `ui.py` assembles at import, and
  the in-app Help is sourced from `wiki/*.md`.
