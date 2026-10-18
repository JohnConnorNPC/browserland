<!-- help:tier dev -->

What 1.0 supports and promises, then the breaking changes, what they cost you,
and how to recover, newest first. Coming from 0.8.0, read the entries from
**Python 3.10 is the minimum** down to **MCP-launched windows start in
`readwrite`**; the ones below those already shipped in 0.8.0.

---

## What 1.0 promises

**1.0 supports** Windows and Linux, Python 3.10 or newer, and current Chrome,
Edge and Firefox.

**Within 1.x:**

- **Mods:** the mod API stays at `ctxVersion: 1`, with additions only. Check
  that a newer member exists before you use it
  ([[Writing a Mod|Writing-a-Mod]]).
- **Saved state:** `/state` blobs written by a 1.x release are read by every
  later 1.x release. Going back to an older release is not covered.
- **MCP tools:** tool names stay, and their arguments only gain optional
  fields.

A change that would break one of these three waits for 2.0. Anything not listed
here can change in a 1.x release; when it does, it is written up on this page.

---

## Python 3.10 is the minimum (1.0)

**What changed.** `pyproject.toml` now has `requires-python = ">=3.10"` (it was
`>=3.9`), so `pip install -e .` refuses a 3.9 interpreter.

**Who is affected.** Anyone running the broker or the agent on Python 3.9. The
`mcp` extra already needed 3.10.

**What to do.** Install Python 3.10 or newer, then reinstall from the checkout
(`pip install -e .` plus your extras). The launcher scripts run the checkout
with whatever `python` or `python3` is on `PATH` and never check its version.
On Linux, `run-broker.sh` and `run-agent.sh` reuse an existing `.venv`, so
delete it after installing the newer Python and the next run rebuilds it.

---

## The peer proxy needs websockets 15 (#252)

A broker that relays to peers now needs `websockets` 15 or later as well as
`httpx` (`pip install -e ".[proxy]"` from the checkout installs both): with an older
`websockets` its whole `/peer/*` prefix answers 503 `proxy_unavailable`, and
the startup line names the missing version. Brokers without peers are
unaffected. See [[Technical-Reference#peer-proxy-252]].

---

## Broker roles: hub and node, and `terminals: false` (#253)

Nothing changes for an existing deploy. A config without the new `terminals`
key keeps launching terminals exactly as before, and `serve_ui` keeps meaning
what it meant: `true` is now called a **hub**, `false` (or `--headless`) a
**node**. Two things are additive and visible:

- **`GET /info`** gains `role` (`"hub"` / `"node"`, derived from `serve_ui`) and
  `terminals`; **`GET /mcp/info`** gains `terminals`. Clients that ignore
  unknown keys are unaffected.
- **A node's `GET /`** answers `{"ui": false, "role": "node"}` instead of
  `{"ui": false}`. A health probe that checks `ui === false` keeps working; one
  that compares the whole body for equality must accept the new key.

The new behaviour is opt-in: `terminals: false` (or `--no-terminals`) makes a
hub refuse new launches with `terminals_disabled`. A node with
`terminals: false` refuses to start. See
[[Technical-Reference#broker]].

---

## MCP-launched windows start in `readwrite` (#243)

**What changed.** A window an MCP client launches (`launch_terminal`,
`POST /mcp/launch`) comes up in the launch's `mode`, or in `readwrite` when it
passes none — scoped or unscoped alike. An unscoped launch used to inherit the
broker's `default_mode`. Every launch also mints a launch handle that lets the
launching agent change that window's scope and mode later (`set_window`,
`POST /mcp/window`).

**Who is affected.** A broker with `allow_launch` on whose operator relied on
`default_mode` (say `read`) to cap what agents can do in the terminals they
launch **unscoped and without a `mode`**: those terminals are now drivable by
their launcher. Scoped launches already started in `readwrite`. With
`allow_launch` off (the default) nothing changes.

**What to do.** If launched windows must start read-only, have the agent pass
`mode: "read"`, or turn `allow_launch` off. Setting the window's mode from its
robot button only revokes write access after the launch (the window comes up
drivable first); a browser edit also clears the launch handle, so the agent
cannot change it back.

---

## Workspaces became a mod, and the layout blob changed shape (#148)

**Nothing to do, and nothing is lost.** This is here because the persisted
layout changes shape on first load, and because the feature can now be turned
off.

### What changed

`prefs._layout` used to group tiled columns under `workspaces:[...]` with an
`activeWs` index, and the tiling core owned that model. It is now a single
desktop:

```
before   { mode, activeWs, workspaces:[ {id, name?, focusedCol, columns:[...]} ] }
after    { mode, focusedCol, columns:[...],
           mods: { workspaces: { version:1, activeId, list:[{id,name?,focusedCol,focusedColId?,focusedColIdAt?}] } } }
```

Every column lives in `columns` exactly once. Workspaces are a mod
(`mods/workspaces/`, **enabled by default**) that stamps a `wsId` on each
column and tells the strip which ones to draw.

### What it costs you

Nothing, on either path:

- **Upgrading.** The first `getLayout()` concatenates your existing workspaces'
  columns into `columns`, in order, and hands the grouping to the mod, which
  rebuilds your workspaces from it. Names and the workspace you were last on
  are preserved. The migration is one-way and idempotent, and it is
  **non-destructive** — no column is dropped even if the mod never loads — so a
  browser that migrates first cannot damage a peer that has not yet.
- **Turning the mod off.** Every column from every workspace appears together
  on one desktop; the pager, the nine workspace shortcuts and the
  workspace menu items go away. Your grouping is kept, not deleted: turn it back
  on and the workspaces return exactly as they were. The same is true of pinning
  it off broker-wide (Control Panel -> Mods on this broker).

Your keyboard bindings for `workspace-prev` / `workspace-next` /
`workspace-1..5` are stored by action id and are untouched by any of this.

### Downgrading

An older build reads the new blob as a layout with no `workspaces` key, which
its `reconcileLayout` heals into one workspace holding **no** columns — your
windows come back as floating windows rather than tiled, and you would rebuild
the layout. Keep a copy of `webterm_state.json` if you plan to roll back.

---

## The token travels in an `Authorization` header (#144)

**What changed.** The page sends the broker token as
`Authorization: Bearer <token>` on every HTTP call, never in the query string.
Only WebSocket dials (a browser cannot set headers on them) and the first
`?token=` deep link still carry it in a URL, and the deep link is scrubbed from
the address bar on load. Cross-origin requests may now need `OPTIONS`
preflights, which a browser caches; the broker advertises a maximum age of
86400 seconds (`Access-Control-Max-Age`).

**Who is affected.** Anyone fronting a broker with a reverse proxy that strips
or rewrites the `Authorization` header, or that does not pass `OPTIONS`
through: every host behind it then looks like a wrong password, and the login
overlay keeps coming back. Scripts calling a broker's own routes are
unaffected — those still accept `?token=` and `?auth=` as well as the header —
but the `/peer/<id>/…` relay (#252) takes the hub's token in the
`Authorization` header only.

**What to do.** Make the proxy forward the `Authorization` header unchanged and
let `OPTIONS` requests through. See [[Technical-Reference#auth-model]].

---

## A token is required on every connection, including loopback (#142)

**This breaks tokenless installs.** It cannot not break them — the whole point
is that a request which used to be accepted is now refused. Read the table
below to find your case.

### What changed

Browserland used to treat **loopback as an auth exemption**. Three problems:

1. **With no `auth_token` configured** — the shipped default —
   `/sessions`, `/profiles`, `/ws` and `/control` were gated by a check that
   did nothing when no token existed. On a `0.0.0.0` or tailnet bind that was
   an open read of every terminal's title, pid and cwd, an open attach to every
   PTY, and an open steal of the single-active-browser lease.
2. **Loopback never meant "same machine."** The recommended topology is
   `tailscale serve` in front of a `127.0.0.1` bind — and a proxied request
   *arrives from loopback*. So the recommended deployment handed the whole
   tailnet an exempt path to `/launch` (shell spawn) and `/file/*` (host-wide
   read/write). A page in your own browser reaches loopback too.
3. **`/browserland` was exempt even when a token WAS configured** — the one
   gate the token never covered. WebSockets are not CORS-gated, so any website
   you had open could dial `ws://127.0.0.1:4445/browserland`, re-register a
   live window id (kicking the real agent off) and inject fabricated output
   into a terminal you trust.

Now: **a token is required on every surface, on every interface, always.**
There is no loopback exemption and no opt-out.

The only unauthenticated responses are `GET /` (the token is typed *into* that
page, and auth is query/header-only with no cookies — gating the document would
401 every reload, bookmark and new tab forever), `GET /help-corpus.json`, and
the `OPTIONS` preflights, which carry no credentials by design — plus two added
later, `GET /vendor/*` (#143) and `GET /mods/<id>/<gen>/<name>` (#163), which a
`<script src>` must load before any token exists. `/mcp/*` keeps its own
separate `mcp_token` realm. See [[Technical-Reference#auth-model]] for the
current table.

**A fresh install still works with one command.** With nothing configured the
broker mints its own token into `webterm_token.json` beside its state store and
prints a ready-to-open `?token=…` URL.

### Does this affect me?

| Your install | What breaks | Recovery |
|---|---|---|
| **Tokenless, loopback only** (the shipped default) | The browser asks for a token on next load. **Terminals launched before the restart are stranded.** Scripts curling `/sessions`, `/file/*` or `/launch` over loopback now get `401`. | The token is printed at startup and by `--print-token`. Relaunch the terminals. |
| **Tokenless behind `tailscale serve` / a proxy** | Same, plus every remote browser must enter the token once. | Same. |
| **`auth_token` already configured** | Almost nothing. Running agents already carry `WEB_TERMINAL_TOKEN` and reconnect fine; browsers already stored it. Only **hand-started local agents** and tokenless local scripts break. | `launchers/run-agent.{sh,ps1}` now read the sidecar automatically, or set `$WEB_TERMINAL_TOKEN`. |
| **MCP clients** (`webterm.mcptool`) | Nothing. Separate `mcp_token`, `Authorization: Bearer`, `/mcp/*` realm — untouched. | — |
| **Node** (`serve_ui: false`, `--headless`) | Nothing. `GET /` still answers `200` with `"ui": false`, so health probes keep working. Since #253 the body also names the role — `{"ui": false, "role": "node"}` — which a probe that checks `ui === false` still reads as "no page". | A probe that compares the **whole** body for equality needs the new key. |

### Getting your token

```bash
python -m webterm.broker --print-token
```

Prints the token and the URL to open, without starting a server. It never
mints one — asking for the token must not be the thing that creates it.

Resolution order, for the broker and for `--print-token` alike:

1. `$WEB_TERMINAL_TOKEN`
2. `auth_token` in `broker_config.json`
3. `webterm_token.json` beside the state store
4. *(broker only)* mint a new one into that file

### The one unavoidable loss: stranded terminals

If you were running **tokenless**, the terminals that broker launched were
spawned with `WEB_TERMINAL_TOKEN` deliberately removed from their environment.
After you restart the broker they cannot re-register.

**Their shells keep running. Their windows never come back.** There is no
server-side rescue: an environment variable cannot be injected into a live
process. The broker notices this happening and, after a few refused producer
connections, logs what it means and points here.

So, before deploying:

- Finish or accept the loss of whatever is running in those terminals.
- Consider setting an explicit `auth_token` in your config **before** the
  cutover, so you know the value in advance rather than discovering it in the
  log afterwards.
- Then restart the broker and relaunch your terminals once. From then on
  everything self-heals: terminals launched by the UI get the token from the
  broker automatically.

### Other behaviour changes

- **The startup banner prints the full `?token=…` URL only on the run that
  minted it, and only to an interactive terminal.** Under systemd, Docker or CI
  it prints a pointer to `--print-token` instead, so a live credential is not
  baked into aggregated logs. Sanic's access log is now off for the same reason
  (the token rides in the query string).
- **A first start against a directory that already holds broker state prints a
  loud `UPGRADE NOTICE`** — that combination means this install used to run
  tokenless.
- **The token is no longer visible from inside your terminals.** The agent
  removes `WEB_TERMINAL_TOKEN` before spawning your shell, so `echo
  $WEB_TERMINAL_TOKEN` is empty. Shell-side automation that needs it should
  read `webterm_token.json` or run `--print-token`.
- **An agent refused for a missing token** retries every 10 s instead of once a
  second, and logs one error naming `$WEB_TERMINAL_TOKEN`. It does not give up
  permanently — restoring the right token lets it reconnect on its own.
- **New response headers:** `Referrer-Policy: no-referrer` (the desktop URL
  carries the token, so an outbound link must not leak it in `Referer`),
  plus `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors
  'none'` (`GET /` is public so login can bootstrap, but it must not be
  embeddable — an attacker page cannot read across origins, yet could otherwise
  clickjack a browser that already holds a token).
- **Error shapes:** the `disabled_no_token` and `launch_disabled_no_token`
  `403`s are gone. Everything answers `401 auth_required`, which is what the UI
  and the mods already match on.

### File permissions

`webterm_token.json` is created `0600` on POSIX. **Windows has no POSIX file
mode**: the file inherits the containing directory's ACL, so on a shared
Windows host make sure the broker's working directory is not readable by other
accounts. The file is git-ignored either way.

If the directory is not writable the broker still starts, but with an
**ephemeral** token: it changes on every restart, `--print-token` cannot
recover it, and terminals do not survive a restart. That case warns loudly on
every start.
