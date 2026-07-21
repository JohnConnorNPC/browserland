# Upgrading

Breaking changes, what they cost you, and how to recover. Newest first.

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
the `OPTIONS` preflights, which carry no credentials by design. `/mcp/*` keeps
its own separate `mcp_token` realm.

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
| **Headless** (`serve_ui: false`) | Nothing. `GET /` still answers `200 {"ui": false}`, so health probes keep working. | — |

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
