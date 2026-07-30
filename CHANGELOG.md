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

### Fixed

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
changes — see [`docs/UPGRADING.md`](docs/UPGRADING.md) for the record of those.

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
- **[`docs/MODS.md`](docs/MODS.md), the mod-authoring guide** (#171) — the `ctx`
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
  see [`docs/MODS.md`](docs/MODS.md) (#163, #171).

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
