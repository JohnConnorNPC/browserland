<!-- help:tier dev -->

**Browserland** (codename `webterm`, still the name of the Python package and its
directories) is a self-contained web terminal system: **headless PTY agents** + a
**WebSocket broker**, speaking Browserland's own web-terminal producer protocol. The
name evokes a place — a little world inside your browser where a whole fleet of
terminals and AI coding agents live, are launched, and are driven from a single
tab. (`webterm` remains the Python package/module name.) The protocol is
deliberately small: any producer that speaks these JSON frames over a single
WebSocket can register with the broker.

```
┌─────────┐  binary ANSI + JSON   ┌────────┐  /ws?session=<id>  ┌─────────┐
│  agent   │ ──────────────────▶ │ broker │ ──────────────────▶ │ browser │
│ (PTY +   │ ◀────────────────── │ :4445  │ ◀────────────────── │ xterm.js│
│  ConPTY/ │  input/resize/      └────────┘  input/paste/resize └─────────┘
│  openpty)│  snapshot_please
└─────────┘
```

## Quick start (this box, Windows)

```powershell
# broker (default 127.0.0.1:4445)
python -m webterm.broker

# an agent running cmd.exe, registered with the local broker
python -m webterm.agent -- cmd.exe

# then open http://127.0.0.1:4445/ and click the session (or "new terminal")
```

Linux:

```bash
./launchers/run-broker.sh                  # broker
./launchers/run-agent.sh -- bash -l        # agent
```

## Components

| Path | What |
|---|---|
| `webterm/protocol.py` | the ONLY place the JSON frame shapes live |
| `webterm/agent/` | headless producer: PTY backend + ring buffer + OSC title sniffer + reconnecting WS client |
| `webterm/agent/backends/linux_pty.py` | `pty.openpty` + `Popen` + `loop.add_reader`; TIOCSCTTY so Ctrl-C works |
| `webterm/agent/backends/win_conpty.py` | Windows PTY via low-level `winpty.PTY` + reader thread (Proactor loop has no `add_reader` for pipes). **Backend auto-selection:** ConPTY silently drops the `0x03` → `CTRL_C_EVENT` translation when the hosting process has no console (verified empirically — exactly the headless cases this agent exists for), so on `auto` (and forced `conpty`) the agent first acquires a hidden console (`AllocConsole` + `SW_HIDE`) and re-enables Ctrl-C (`SetConsoleCtrlHandler(None, FALSE)`, undoing `CREATE_NEW_PROCESS_GROUP`'s disable — #25), and detached agents then run ConPTY; `auto` falls back to WinPTY only when acquisition or the re-enable fails, and `winpty` forces the legacy backend without acquiring a console. |
| `webterm/agent/snapshot/raw.py` | tier-1 snapshot: `ESC[0m ESC[2J ESC[H` + ring replay |
| `webterm/agent/snapshot/pyte_snap.py` | tier-2 (optional, `--snapshot-mode pyte`): replay ring through pyte, render the settled grid |
| `webterm/broker/` | Sanic app: desktop UI page, `/ws` relay, `/browserland` producer WS, `/sessions`, profiles-only `POST /launch` |
| `launchers/` | venv-bootstrapping run scripts for both OSes |

## Agent

```
python -m webterm.agent [opts] [--] command...
  --broker-url URL     $BROWSERLAND_BROKER_URL > flag > ws://127.0.0.1:4445/browserland
  --auth-token TOK     $WEB_TERMINAL_TOKEN; appended as ?token= (REQUIRED
                       for every broker since #142, loopback included)
  --cols/--rows        initial PTY size (default 80x24)
  --title T            initial title (default: command basename)
  --window-id N        pin the session id (default: random 48-bit)
  --ring-bytes N       snapshot ring cap (default 262144)
  --snapshot-mode raw|pyte
  --cwd DIR
```

The agent's exit code is the child's exit code. While the broker is down
the PTY keeps running; the client reconnects with exponential backoff
(0.5 s → 10 s cap, ×2 per failure, reset on success) and re-hellos with the
*current* title/dims. Missed bytes are not replayed — the browser's attach
triggers `snapshot_please`, which heals from the ring.

**`psutil` is an optional, best-effort dependency** (`pip install -e ".[procs]"`),
not declared in core deps. It powers the task-manager process list
(`enumerate_procs`), the foreground-agent badge (`foreground_command`), and
live-cwd tracking (`cwd`) — each degrades to empty/None without it. Destroying
a window also prefers the psutil path (identity-checked `create_time` guard),
but the Linux backend's `kill_proc_fallback` kills the shell's whole POSIX
session by SID (the shell is its own session leader via `start_new_session`,
disjoint from the agent's session) when psutil is absent, so "destroy window"
works either way. Windows without psutil still returns `psutil_unavailable`
for destroy (no fallback yet).

## Broker

```
python -m webterm.broker [--host 127.0.0.1] [--port 4445] [--config PATH]
                         [--role hub|node] [--headless] [--no-terminals]
```

**Roles (#253).** A broker is a **hub** or a **node**. A hub serves the desktop
page; you open it in a browser. A node serves the JSON/WS API only, for another
Browserland to reach. The role is **derived** from the stored key `serve_ui`
(default `true` = hub) and never stored itself. `--role node` (or a config
`"role": "node"`, accepted as an alias and folded into `serve_ui`) means
`serve_ui: false`; `--headless` is the older spelling of `--role node`. A node
serves the full JSON/WS API but **not** the desktop page or the in-app Help —
`GET /` returns `200 {"ui": false, "role": "node"}`, `/help-corpus.json` 404s,
and both UI constants (`INDEX_HTML`, `HELP_CORPUS`) are never assembled (#87).
The CLI overrides the config (like `--host`/`--port`); there is no
`--no-headless`. `--headless` together with `--role hub` is a contradiction and
refused, as is a config `role` that disagrees with an explicit `serve_ui`.

The second stored key, `terminals` (default `true`), says whether this broker
hosts terminals. `--no-terminals` sets it `false`: the broker then refuses new
launches — `POST /launch`, `POST /mcp/launch`, `POST /profiles/config` and
`GET /profiles/detect` answer **403 `terminals_disabled`** — while terminals that
already exist keep running and `WS /browserland` keeps accepting agents, so an
agent launched before the flag reconnects as usual. Profile reads
(`GET /profiles`, `GET /profiles/config`) and `/sessions` still answer. Use it on
a hub that is only the front door to its nodes. A node must host terminals:
`serve_ui: false` with `terminals: false` is refused at startup — one line on
stderr, exit 2, before the port is bound (`--print-token` and
`--scan-recordings` still run on that config; they bind nothing).

`GET /info` reports `serve_ui`, the derived `role` (`"hub"` / `"node"`) and
`terminals`; `GET /mcp/info` reports `terminals`. The boot log names both
(`broker role: hub - terminals on`). Existing JSON/WS behavior is otherwise
unchanged in every combination; a broker that predates #253 omits `role` and
`terminals`, which a client reads as an ordinary broker that launches.

Config (`broker_config.json`, path overridable via `$WEB_TERMINAL_CONFIG`;
see `broker_config.example.json`): `auth_token`, plus `agent.profiles` for
`/launch`. **Profiles only** — `/launch` accepts
`{"profile": "cmd", "cols": 120, "rows": 32, "title": "..."}` and never a
client-supplied command/cwd/env. Responses: `200` registered, `202` spawned
but no hello within 10 s, `400` unknown profile, `401`/`403` auth, `429`
too many pending, `500` agent exited early.

**`GET /update/check?refresh=1`** re-asks GitHub through the daily cache; the 30-minute poll never sets it, and only a literal `1` counts. Because a forced refresh bypasses the TTL it bypasses the quota strategy with it, so the broker floors it at one per 60 s and caps it at six per rolling hour, leaving most of GitHub's 60/hour-per-source-IP for everything else on that machine. An active rate-limit negative cache outranks the button entirely — the reset GitHub named in a header is honoured, not clicked through. Admission is decided and reserved **inside** the same lock the fetch holds (checking before the lock would let a queue of requests each pre-authorise themselves and then fetch in turn), and attempts are stamped before the request goes out, since a failed fetch spends the same quota as a successful one. A refresh that finds a newer publish than the one it queued behind **coalesces** rather than re-asking, tracked by a publish serial read before the lock. A refused refresh answers `200` with the cached result plus `refreshed: false` and `refresh_refused: {reason, retry_after_s}` — never an error, and never a bare `200` a client could render as "just checked".

### Update apply (#182 Part 2)

**`POST /update/apply` applies the previewed upstream commit**, then exits with code 75 so the supervisor restarts into the new code. It is **manual-trigger only** — no schedule, no timer, no auto-install. Requires all three of `update_check_enabled`, `update_apply_enabled`, and `restart_enabled` to be true. Each resolves independently: a key **present** in `broker_config.json` (either `true` or `false`) owns that one gate and locks it against the GUI; an **absent** key leaves the gate to a sidecar (`webterm_update_policy.json`) written by `POST /update/policy` — the desktop's one-click consent when the update mod is turned on grants whichever of the three the config hasn't already claimed, and the Update window's "Allow this broker to update itself" row grants apply + restart together afterward. `POST /update/policy` takes any **subset** of the three keys; keys it does not name keep whatever the sidecar already had, so two operators granting different gates never clobber each other. A corrupt sidecar fails every gate it would otherwise decide closed, never through to a default.

The route takes `{"target_sha": "<40-hex>"}` — exactly the commit the UI previewed in the last successful check. The server re-proves every precondition fresh:

- The target commit is not some random hash — it is the one the latest check named as ahead of / current-with upstream.
- The working tree is clean (no staged/unstaged changes; uncommitted work must be committed or stashed).
- No local commits would be discarded (if the check said "3 behind", applying is safe; if it said "2 ahead / 3 behind", applying would discard work).
- Dependencies match or are acceptable (a dependency added/removed by the update is reported so the operator can decide whether to install/uninstall first).
- Restart is possible (the gate is on, the supervisor exists, the mechanism is available).

The git phase lives in `webterm/broker/update.py`'s marked mutation section: `git fetch <explicit https URL> <ref>` (never `git pull`, never a remote name — a fork's `origin` must be irrelevant), then `git merge --ff-only <sha>` with hooks disabled. Objects are fetched with a 120-second timeout; the merge with a 60-second timeout. Stderr is captured for reporting; the report names every locked file on Windows, bounded to 2000 bytes.

Failures are reported **accumulating every reason** — the UI shows all unmet preconditions in one pass rather than trial-and-error. The response is `202` with a `last_deploy` section surfaced in the next `/update/check` so the UI can report the outcome.

**The broker that was running the old code exits and is replaced.** A rollback is automatic: if the new broker never comes up (supervisor restarts it, but it exits again before a worker-ready timeout), the supervisor automatically reverts to the recorded prior commit sha (held in `deploy.journal` beside the checkout). The outcome (`rolled-back`, `deploy-failed`, etc.) rides in the next check response under `last_deploy.outcome`, visible to the operator.

**Agents survive the restart** and reconnect to the new worker, but terminals do not re-execute: they keep running the old code they imported. The UI offers a **Fresh terminal** button to spawn a new one. Agents (AI coding assistants) also survive, but any in-memory state (context, plan, accumulated tokens) is lost — the restart is opaque to them, so they reconnect to an empty board.

The `deploy.journal` records every apply attempt (sha before, sha after, outcome, why it failed if it did) outside the checkout, so `git gc` cannot erase it. Rollback success is proved by a changed `bootId` (reported in `/info`); rollback failure is kept as `rollback-impossible` and requires manual repair.

### Broker restart (#183)

**`python -m webterm.broker` is now a supervisor** that spawns the actual server as a worker subprocess. The launcher scripts (bash and PowerShell) and the systemd unit all use `exec` to start the supervisor, making it the main process — there is no wrapper loop. When restarted from the UI, the supervisor stays alive and re-spawns the worker, a *broker-generation restart*: the server process is replaced with fresh code, but agents reconnect to the new worker and survive. It is not a service restart — the launcher preamble and systemd unit environment are not re-run.

Restart is **opt-in**, resolved the same per-key way every update gate is: if `broker_config.json` names `restart_enabled` (either `true` or `false`), that key owns the gate and only editing the file can move it; if it is absent, the gate is the desktop's to grant, via the Update window's "Allow this broker to update itself" row (which grants `apply_enabled` and `restart_enabled` together — see the update mod's own docs). A restart from the UI drains gracefully (stops accepting new work, waits for in-flight critical writes to finish or time out, disconnects idle sessions), and the confirm dialog reports session continuity: guaranteed (agents that survive), at_risk (lost), and unknown. A per-boot cooldown (`restart_cooldown_seconds`, default 90, `0` disables — also config-file only) refuses back-to-back restarts; 90 is deliberately longer than the supervisor's 60-second budget window, so deliberate restarts can never exhaust the crash budget.

The mechanism is a sentinel file and exit code 75: the worker exits 75 (normally reserved for another purpose — this supervisor owns it now) **and** writes a nonce-matched intent sentinel. The supervisor relaunches only if the sentinel matches, refuses any exit-75 without a sentinel, and treats an unarmed 75 as a crash. A restart can happen from systemd (`Restart=always` or `on-failure`, not `on-success`), a Windows scheduled task (`Stop-ScheduledTask` kills abruptly — graceful drains do not complete), or `python -m webterm.broker` by hand (no restart). The supervisor has a restart budget — five relaunches inside 60 seconds with exponential backoff (0.5 s initial, doubling per crash, clamped at 30 s, reset only when a worker stays up 10 s). A crash loop that exhausts the budget exits with a non-zero code; systemd's own `StartLimitAction` can then step in.

`GET /info` reports `restart.available` (can we restart now), `restart.mechanism` (supervisor / systemd / none), `restart.bootId` (this process's id — changes on restart, lets a client confirm it happened), and session continuity. A client confirms a restart by watching `bootId` change.

### Which build is running (#22)

There is one build id in this project: `webterm.build_version()` — the package
version from `webterm/__init__.py` (the **single source**; `pyproject.toml`
declares `dynamic = ["version"]` and reads that attribute) plus the git short
hash when running from a checkout, e.g. `0.8.0+ba4b62e`. It is derived from the
package's own directory (never the process cwd, so a wheel installed inside an
unrelated repo cannot report that repo's commit), cached per process, and never
raises.

It rides the agent hello, `GET /info` and `GET /mcp/info` — and it is baked into
the served desktop page:

```html
<meta name="webterm-build" content="0.8.0+ba4b62e">
```

A broker serves the page it assembled at **import**, so a `git pull` changes
nothing until the process is restarted. That is the gap this closes:

```
curl -s http://host:4445/ | grep webterm-build
git -C /path/to/checkout rev-parse --short HEAD
```

The stamp's `+hash` matches → the running process is the pulled code; it does
not → a restart is still pending. The stamp describes the **running process**,
not the checkout on disk, exactly because `build_version()` is cached at import
and never recomputed per request.

Read it for what it is: the revision `HEAD` pointed at when that process first
asked. It cannot see uncommitted edits (there is no dirty marker — two different
dirty trees would share a hash, which is a worse signal than none), and if a
pull lands *while* a broker is running, modules imported before and after it
come from different revisions and no single stamp can say so. A caching reverse
proxy in front of `GET /` will also happily serve you an old stamp.

It is a `<meta>` in the head and deliberately **not** a value inside the page's
one inline script: `script-src` authorizes that script by `sha256` of its exact
bytes (see [Auth model](#auth-model)), so a per-commit string in there would
change the CSP hash on every commit. Script that wants the value reads the tag —
`document.querySelector('meta[name="webterm-build"]').content`.

### Auth model

**A token is required on every surface, on every interface, always** (#142) —
no loopback exemption, no opt-out. Resolution (never returns nothing):
`$WEB_TERMINAL_TOKEN` → config `auth_token` → the `webterm_token.json` sidecar
beside the state store → **mint** `secrets.token_urlsafe(32)` into it. Compared
with `hmac.compare_digest`. `python -m webterm.broker --print-token` reports it
without minting or starting a server.

Loopback was never a sound exemption: `tailscale serve` in front of a
`127.0.0.1` bind (the topology [[Setup-and-Onboarding]] recommends) makes every
tailnet request arrive *from* loopback, and any page in the user's browser
reaches loopback too. Upgrading a tokenless install is a **breaking change** —
see [[Upgrading]].

**How the token travels (#144).** HTTP sends it as `Authorization: Bearer`, never in the
query string. A URL credential leaks where a header does not: any script on the page can
read the full URL out of `performance.getEntriesByType('resource')`, a DevTools HAR export
carries it into a bug report, and a reverse proxy logs it. There is deliberately **no**
client function that builds a token-bearing HTTP URL, so one cannot be reintroduced by
accident — `hostFetch(host, path, opts)` is the only way to call the API.

Two places still carry `?token=`, both unavoidable and both deliberate:

* **WebSocket dials** (`/ws`, `/control`, `/browserland`). The browser WebSocket API
  cannot set request headers on the handshake, so `hostWsUrl` appends the token. A host
  reached through this broker is the exception: it dials with a one-time ticket instead
  (see [Peer proxy → Sockets](#sockets)), so no token rides that URL.
* **The `?token=` deep link** (`http://host:4445/?token=…`). It is how a token reaches a
  fresh browser at all; it is adopted into localStorage and scrubbed from the address bar
  via `history.replaceState` on load.

So this narrows the exposure from every request to the handshake and the first load — it
does not eliminate it. Operationally: an `Authorization` header makes a cross-origin
request non-simple, so remote hosts pay an `OPTIONS` preflight; the broker answers every
one and sets `Access-Control-Max-Age: 86400`, making it one round trip per host per day
rather than per request. `ACAO: *` stays legal because a header set explicitly by
`fetch()` is **not** a credentialed request in the CORS sense (that needs
`credentials: 'include'`), which is also why a wrong token still returns a *readable* 401
instead of an opaque failure — that is what keeps the login overlay working. **If you
front the broker with a reverse proxy, it must forward the `Authorization` header**; a
proxy that strips it makes every host look like a wrong password.

The mint is `O_CREAT|O_EXCL` (0600 on POSIX; Windows inherits the directory
ACL), not an atomic replace: two brokers racing on one state dir both see no
file, and last-writer-wins would leave the loser running a token that is not
the one on disk. The O_EXCL loser re-reads and adopts the winner. An unwritable
directory yields an *ephemeral* token — the broker still starts, but warns on
every boot that it changes on restart and `--print-token` cannot recover it.

| Surface | Rule |
|---|---|
| `GET /`, `GET /help-corpus.json` | **unauthenticated.** The token is typed *into* that page and auth is query/header-only with no cookies, so gating the document deadlocks the bootstrap — every reload, bookmark and new tab would 401 forever. Neither carries host- or session-derived data. On a node (`serve_ui: false`), `GET /` is `200 {"ui": false, "role": "node"}`, so health probes keep working. The corpus is public but **auth-sensitive** (#173): with no token it is the wiki + shipped-mod corpus alone, exactly what it was before #163; the sections contributed by **installed** mods — their help text, their manifest label/icon, and hence the list of installed ids — are merged in only for a caller holding the token. Same `request_token_ok` as every gated route, answering with a smaller `200` instead of a `401`, plus `Cache-Control: no-store` and `Vary: Authorization` so no cache can hand one audience the other's body. |
| `GET /vendor/*`, `GET /mods/<id>/<gen>/<name>` (#163) | **unauthenticated, and forced rather than chosen**: the browser needs the vendored xterm to render the *login* page, before any token exists, and a `<script src>` cannot carry an `Authorization` header (`?token=` is structurally banned by #144). Both serve from an in-memory allowlist dict, so a client-supplied name can never reach the filesystem. Consequence: an **installed mod's source and styles are publicly readable**, exactly as `GET /` already carries every shipped mod's source — but *reaching* one takes the id **and** the generation **and** the file name, and #173 stopped the help corpus from handing out that first ingredient. Neither ingredient is a *broker* secret, though: `<gen>` is a content hash of the package, so someone holding a **publicly distributed** mod's bytes recomputes it and can confirm that mod is installed here (`200` vs `404`). #173 stops the corpus **enumerating** installed ids; it cannot hide a mod you already know to ask for by name. `help.md` is not servable here at all. |
| `OPTIONS` preflights | unauthenticated by design (they carry no credentials). Explicit routes, because route resolution happens before request middleware. |
| `WS /browserland` (producers) | token required, loopback included. Was the one gate the token never covered — and WebSockets are not CORS-gated, so any website could dial `ws://127.0.0.1:4445/browserland`, re-register a live `window_id` (kicking the real agent off with 1012) and inject fabricated terminal output. Refusal is a post-upgrade WS close **4401** (an HTTP reject would surface as an opaque 1006). A re-register whose `host` and nonzero `pid` match the entry it replaces now keeps that window's MCP access override instead of falling back to the broker default; that match is a collision heuristic, not a security boundary (both are self-reported), and the producer token is the boundary. |
| `POST /launch` | token required — never an open RCE on any bind. Refusal order: **401 `auth_required`**, then **503 `restarting`** while the broker quiesces, then **403 `terminals_disabled`** on a broker with `terminals: false` (#253) — checked before the body is parsed. |
| `WS /ws`, `WS /control`, `GET /sessions`, `GET /profiles` | token required (`?token=`, `?auth=`, or `Authorization: Bearer`). Always **401 `auth_required`**, never 403: the login overlay and the taskbar host chips pop on 401 only. |
| `GET`/`POST /profiles/config`, `GET /profiles/detect` (profile editor, #70) | browser token, same as `/file/*` and `/mcp/config`. Full commands are browser-realm only; `/profiles` and `/mcp/profiles` stay names-only. |
| `GET`/`POST`/`PUT /peer/<id>/…` (peer proxy, #252) | **two gates**, see [Peer proxy](#peer-proxy-252): browser routes take **this broker's token as the bearer** (header only — a `?token=` does not count here) **plus** the peer's own token in `X-Browserland-Peer-Token` and the pairing in `X-Browserland-Peer-Origin`; `/peer/<id>/mcp/*` takes the **peer's MCP token alone**, and only for a peer configured with `mcp: true`. `OPTIONS` is not routed (405). |
| `POST /peer/<id>/ws-ticket`, `WS /peer/<id>/ws`, `WS /peer/<id>/control` (peer proxy sockets, #252) | the mint takes the **browser gate** above and answers a one-time ticket; the two socket routes take **only** `?ticket=` — no token of any kind — and close **4403** on a bad, expired, reused or revoked one. See [Sockets](#sockets). |
| `GET /status/fetch` (AI-provider status proxy, #112) | browser token, same as `/info` and `/state`. One of the broker's three HTTP-library egresses (`/status/fetch`, `/update/check`, the configured peers) plus the update apply's `git fetch` subprocess: an allowlist of AI-provider Atlassian Statuspage hosts, reached by provider **id** (the client passes ids, never a URL — unknown ids are dropped, an all-unknown request is `400`). Each fetch is **https-only, no-redirect, no-proxy, `200`-only, 512 KiB-capped, 4 s timeout**, with a 60 s per-id cache; any failure degrades to an `unknown` row (never blocks the UI). **Privacy**: enabling the (default-off) `aistatus` mod is what turns this on — the broker's egress IP then becomes visible to those status hosts, so it ships disabled until you opt in. |
| CORS (JSON API) | `Access-Control-Allow-Origin: *` **unconditionally on every response**, including the 401 — lets the multi-host UI on another broker's origin read this one, and without it a cross-origin login probe surfaces as a fetch TypeError ("wrong password" indistinguishable from "host down"). Auth is token-in-query/header and never cookies, so `*` grants no ambient credential; the real gate is the token on every route. |
| Response headers | `Referrer-Policy: no-referrer` (the desktop URL carries the token, so an outbound link must not leak it in `Referer`), `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` (`GET /` is public so login can bootstrap, but an attacker page must not be able to iframe the real UI and clickjack a browser that already holds a token), and CSP `script-src` — see below. |

**Third-party code (#143, #146).** The page used to pull xterm from jsdelivr and, lazily, CodeMirror from esm.sh — third-party code executing in the same origin that holds `prefs._hosts[].token` for **every** configured host, tokens that gate `/launch` and host-wide `/file/*`. Both are now vendored, and **no third-party origin executes in the page at all**:

* **xterm is vendored** into `webterm/broker/vendor/` and served by the broker at `/vendor/*` (public, like `GET /` — the browser needs it to render the login page, before any token exists). There is no CDN to compromise, no SRI hash to keep in sync, and the terminal now works **offline**, which it never did before. The bytes are the exact published npm files, pinned by sha384 in `tests/test_vendor_assets.py`; an upgrade is a commit that changes both. Served from an allowlist dict, so a client-supplied name can never reach the filesystem.
* **CodeMirror is vendored** into `webterm/broker/vendor/codemirror/` and served at `/vendor/codemirror/*` by a second route with a literal segment (so the lookup is still one dict get on a prefixed key — a name can't express a path). The text editor works offline too. See below for why it took its own generator.
* **`script-src`** allows only `'self'` and a `'sha256-…'` for the page's single inline `<script>`. The hash is computed at startup from the assembled `INDEX_HTML` itself (`ui.inline_script_hash`), so it tracks the bundle automatically and needs no `'unsafe-inline'` — meaning an **injected** inline script cannot execute either. Nothing else is set (no `default-src`/`style-src`/`connect-src`): those would have to enumerate every inline style, the `data:` favicon, `blob:` download URLs and every federated host, each a way to break the app for no gain here.

**Why CodeMirror needed a generator (#146).** xterm was four self-contained files with exact-pinned URLs — download, hash, commit. CodeMirror 6 is ~116 modules across 40 npm packages that must share **one** instance each of `@codemirror/state`, `view` and `language`; a second concrete build of `state` throws and drops the editor to a plain `<textarea>`, while a second `view` or `language` is **silent** — syntax highlighting just stops. That is why `CM_VER` in `mods/editor/codemirror.js` uses semver **ranges**: esm.sh collapsed every range onto one build at request time. Mutable URLs meant nothing could be hashed, which is why esm.sh stayed in `script-src` after #143.

`python -m webterm.broker.vendor_codemirror` moves that guarantee from request time to **generate time**. It parses `CM_VER` (still the single source of truth for what the editor loads), crawls the graph from esm.sh keying every module by its final resolved URL, rewrites each import specifier to a relative sibling path, and **refuses to write anything if any package appears twice**. Run it by hand on upgrade; `--check` re-verifies the committed tree offline. Two details worth knowing:

* It only rewrites the module **prologue**, not the whole file. `@codemirror/lang-javascript` ships completion snippets whose payload is literal JavaScript source (`p('import {${names}} from "${module}"…')`) — a file-wide regex matches inside that string and corrupts it. Static imports are top-level-only and esbuild hoists them, so the prologue is where they all are; the generator then asserts nothing package-path-shaped survives past it.
* The first run found `autocomplete`, `lang-html` and `lang-javascript` each loading **twice** — our exact pins had drifted behind what the transitive `^` ranges resolved to, so esm.sh was serving both. Those three became ranges. The assertion that caught it now runs in `tests/test_vendor_assets.py` against the committed graph.

Per-file sha384s live in the generated `vendor/codemirror/manifest.json` (which is also the served allowlist); only the manifest's own hash is pinned in the test file, so an upgrade is one hand-updated constant. Like the xterm pins, that is **change-control friction, not a security guarantee** — it makes a silent change impossible, not a deliberate one.

**Recordings are stored gzipped (#159).** A recording is JSONL carrying base64 PTY output —
about the most compressible thing the app produces, and an always-on capture rolls a fresh
50 MiB segment forever. New recordings land as `<id>.blrec.gz` (measured ~8x smaller);
`<id>.blrec` keeps meaning an older uncompressed file and is never rewritten. **The suffix
is the encoding** — nothing sniffs content, so a corrupt `.gz` reports as a corrupt gzip
rather than being retried as JSONL. Each uploaded chunk is compressed as its own gzip
*member*, which keeps appends streaming and commit a plain `os.replace`; concatenated
members are valid gzip and Python/GNU tooling reads them transparently. `GET /recording`
still serves **plain JSONL** on the wire, deliberately: HTTP client decoders commonly stop
at the first member (httpx does), so passing the stored bytes through under
`Content-Encoding: gzip` would silently truncate multi-chunk recordings. Disk was the
growth problem; the wire is unchanged.

**Auditing recordings for secrets (#145).** A recording captures the terminal's output
byte-for-byte, so anything the terminal *echoed* is in it — a secret pasted onto a visible
command line, an API key printed by a config dump, or this broker's own token if someone
ran `--print-token` while recording. Recordings are durable and downloadable, so:

```bash
python -m webterm.broker --scan-recordings          # 0 = clean, 1 = found, 2 = incomplete
python -m webterm.broker --scan-recordings --json   # machine-readable
python -m webterm.broker --scan-recordings --secret <OLD_TOKEN>   # rotated token
```

It resolves the live `auth_token` and `mcp_token` **read-only** (auditing must never mint
one — that would search for a value the broker isn't even running) and streams each file,
**base64-decoding `d` before searching**. That decode is the whole point: a plain
`grep <token> *.blrec` finds nothing even when the token is present and returns a
confident false all-clear. Output payloads arrive in arbitrary PTY chunks, so the scanner
carries a `len(secret)-1` byte tail across events — a secret split over two (or twenty)
events is still found, and one that straddles a resize/gap event is too. It scans **both
encodings**; a compressed recording it could not open would be a live secret that quietly
stopped being findable, so a file that fails to decompress is reported as an error
(exit 2, incomplete) and never skipped into a clean result. By hand, the compressed
equivalent of the futile `grep` above is `zgrep` / `gzip -dc … | grep` — equally futile,
and for the same base64 reason.

**Its limits are printed on every run, including clean ones**, because a tool like this
recreating the false all-clear would be worse than not having it: it finds a secret only
where it appears as *contiguous bytes*. A secret the terminal broke up — interleaved with
colour/cursor escapes, redrawn by the shell as you typed, echoed a character at a time —
is not found, and a rotated secret is only searched for if passed with `--secret`. It is
read-only: recordings are archived artifacts and nothing here rewrites one.

Tokens are passed to spawned agents via **env only** (never argv — visible
in process lists), and auth failures log only path + client IP (the token
rides in query strings — Sanic's access log is pinned off for the same
reason). The agent then **removes** `WEB_TERMINAL_TOKEN` before spawning the
user's shell, so a terminal cannot `echo` the credential that gates host-wide
file access and shell spawning.

An agent refused with 4401 backs off to the 10 s cap (the rejection arrives
*post*-upgrade, after the backoff has already reset, so the naive loop retries
at ~1 Hz forever) and logs one error naming `$WEB_TERMINAL_TOKEN`. It keeps
retrying rather than giving up: the token can come back without the process
restarting.

**Browser login**: the token doubles as the UI's password. The page is
served ungated; a login overlay probes `/sessions` and stores the token in
**localStorage** (per browser, per host). `?token=`/`?auth=` URLs still
work as deep links — the token is adopted into localStorage and then
scrubbed from the URL via `history.replaceState` (behavior change from the
URL-persistence era: copied links no longer carry auth; `?session=` deep
links survive the scrub).

Note: the broker pins `app.config.AUTO_EXTEND = False`. sanic-ext, when
merely installed, silently injects its own CORS middleware and an
unauthenticated `/docs` + `/openapi.json`; CORS here is hand-rolled and
token-gated instead.

### Multiple hosts

Control Panel → Hosts lets the UI attach to sessions on additional brokers
(e.g. a WSL box over Tailscale). By default the **browser connects
directly** to each host (cross-origin `fetch /sessions` + `ws://host/ws`);
a host can instead be reached **through this broker** (below). Hosts and
their passwords live per-browser in localStorage. Requirements for a
direct host:

* you need the remote broker's token (every broker has one — either
  configured or minted; `python -m webterm.broker --print-token` prints it)
  and a way in: either a non-loopback bind (plain http), or — recommended —
  a loopback bind fronted by `tailscale serve` / an https reverse proxy.
  Setting `auth_token` explicitly is worth it across a fleet so every host
  shares one password instead of a per-machine minted value;
* **both brokers must run this webterm version** — a pre-CORS remote reads
  as a red "down" broker even when it's running;
* one scheme for the whole fleet — an https page fetching an http remote
  is blocked as mixed content, so cockpit + hosts must be all-https
  (preferred: unlocks secure-context features like clipboard image paste)
  or all-plain-http (see SETUP.md → Multiple machines over Tailscale).

A single broker's status chip is shown in the taskbar by default, even
healthy and local. Since the per-broker actions moved onto the (+)
menu's rows it is a pure indicator, so the browser-global
`hostStatusChip` setting can switch it to `attention` (drawn only while
some broker is auth-needed / down / lease — a broker the user HID does
not count) or `never`; hiding is a `hide-broker-chip` body class, never
an inline display, so the node stays put as the ws-pager's anchor and
eats no taskbar gap slot.
Four states: green ok / red down / amber password-needed / blue lease;
an ok/down chip toggles that host's window visibility, while auth and
lease chips are click-to-log-in / take-over. With two or more brokers
the chips collapse into one worst-state aggregate badge whose click
opens the start (+) menu, where each broker has a live status row with
the same per-host actions (the hide toggle keeps the menu open).
Window prefs are keyed per host; a down host never closes or re-dials
another host's windows.

Remote agents against this broker:

```bash
BROWSERLAND_BROKER_URL='ws://broker-host:4445/browserland' \
WEB_TERMINAL_TOKEN='...' ./launchers/run-agent.sh -- bash -l
```

Any producer can register the same way:
`BROWSERLAND_BROKER_URL=ws://host:4445/browserland?token=...`.

### Peer proxy (#252)

A broker can relay an allowlisted set of **another** broker's HTTP routes, so a
page or a phone that can reach only this broker (say, the public one) still
reaches a broker behind it (on its LAN or tailnet), and that broker needs no
upgrade and no CORS. This broker stores **no credential** of the peer's
(a [socket ticket](#sockets) holds the peer's token in memory until it is
used or its 30 s run out). The
operator names each peer in `broker_config.json`:

```json
"peers": {"b": {"origin": "http://100.64.0.2:4445", "mcp": false}}
```

* **The peer list is validated at startup** — a bad one is one stderr line and
  exit 2, like a bad role. An id is 1–64 of `A-Za-z0-9_-`; an origin is
  `http` or `https` with a host and an optional port: **no path, no query, no
  fragment, no userinfo**, and never this broker itself (loopback or the bind
  host on this broker's port). It is stored rebuilt (`HTTP://B:80/` →
  `http://b`). Unknown keys are refused. `mcp` defaults to `false`.
* **The client names a peer id, never a URL.** The origin is read from the
  config at dial time. `GET /info` publishes `peers` as `{id: origin}` (never
  the `mcp` flag, never a token) and `peer_proxy` (true when this broker has
  peers **and** the proxy extra).
* **The `proxy` extra.** The upstream clients are `httpx` and `websockets`
  15 or later (its asyncio client, for the [sockets](#sockets)), installed by
  `pip install webterm[proxy]`; core stays `websockets` 12 or later +
  `sanic`. One check covers HTTP and sockets: a broker that names peers
  without both logs one line naming what is missing
  (`peer proxy unavailable: install webterm[proxy]: websockets 13.1 < 15; …`)
  and answers every `/peer/*`, the ticket mint included, with
  **503 `proxy_unavailable`**.

**The two gates.** The first decoded segment after `/peer/<id>/` decides:

| Gate | Credential | Refusals, in order |
|---|---|---|
| browser routes | `Authorization: Bearer <this broker's token>` (the header only) **and** `X-Browserland-Peer-Token: <the peer's browser token>` **and** `X-Browserland-Peer-Origin: <the origin that token was paired with>` | 401 `auth_required` (this broker's token) → 503 `proxy_unavailable` → 404 `unknown_peer` → 404 `peer_hop_refused` / `not_relayed` → 401 `auth_required` (no peer token) → **409 `peer_origin_changed`** (the pairing header is missing or names another origin) → 400 `bad_scope` (a repeated scope header) → 503 `peer_busy` |
| `/peer/<id>/mcp/*` | `Authorization: Bearer <the peer's MCP token>` and nothing of this broker's; `X-Browserland-Peer-Origin` optional | 401 `auth_required` (no bearer — checked before any lookup) → 404 `unknown_peer` (also for a peer without `mcp: true`) → 503 `proxy_unavailable` → **409 `peer_origin_changed`** (only when the pairing header is sent and names another origin) → 404 `not_relayed` → 400 `bad_scope` → 413 `body_too_large` / 400 `bad_length` / 408 `body_timeout` (the body is read whole first) → 503 `peer_busy` |

Every refusal happens before anything is dialed. Browser-gate bodies are
`{"ok": false, "error": …}`; MCP-gate bodies are `{"error": …}`, the MCP
realm's shape. Each of this broker's own refusals also carries
`X-Browserland-Peer-Refusal: <error>`, a header a relayed answer can never
carry, so the page acts on this broker's refusals and never on a body a peer
wrote. The 409 is what keeps a token on the machine it was entered for: a
page that has not seen a re-pointed origin cannot send that token to the new
one. Without a bearer the MCP prefix says nothing about which peers exist;
with one, a peer that opted in answers with the peer's own refusal, which
this broker cannot hide (it cannot check the peer's MCP token). Turning `mcp: true` on publishes the peer's MCP surface at this
broker's address; an agent then adds `http://<this broker>/peer/<id>` as a
second mcptool host with the **peer's** MCP token. This broker's MCP token,
scopes and admin credential do not apply to it. That host entry may carry
`"peer_origin"`: the mcptool server then sends it, in this broker's own
canonical form (`http://B:4445/` → `http://b:4445`), as
`X-Browserland-Peer-Origin` on every call, so a re-pointed peer is refused
here instead of receiving the token. A value that is not an origin (a path,
a query, userinfo, surrounding spaces) is refused when the server starts,
naming the host; it is never trimmed. Without it, nothing is sent and
nothing changes.

**The page.** A host record reaches a peer when it carries `via: "server"`,
`peerId` and `peerOrigin` (the origin its token was paired with); only the
Hosts row's mode control writes them (a registry import never does).

* **Switching** keeps the token only where it was paired with the address
  dialed afterwards: direct → peer when the host's url is the peer's
  published origin, peer → direct when `peerOrigin` is the url. A loopback
  or unspecified host (`localhost`, `127.0.0.0/8`, `[::1]`, `0.0.0.0`,
  `[::]`) never keeps it. A switch drops the learned broker id and the OSC 52
  grant, and re-dials that host's sockets.
* **OSC 52** grants are keyed `[id, url]` for a direct host (unchanged) and
  `[id, url, peerId, peerOrigin]` for a proxied one, so a re-pointed,
  re-paired or switched peer loses its grant.
* **Whose 401.** This broker checks its own bearer first, so a 401 carrying
  `X-Browserland-Peer-Refusal` on a request that sent a peer token is this
  broker refusing its own password: the page opens **this** broker's login
  and answers the caller with its own `hub_auth_required` (status 503, made
  in the page, never sent by a broker). An unmarked 401 is the
  peer's own; a marked one with no peer token sent is left to the peer's
  login (it needs a password either way). A wrong token before `/info` was
  ever read fails that read, and the host reads as down.
* **`/info` is read again** when this broker's `/control` status shows a new
  `bootId` (it restarted, and its `peers` may have changed).
* **No `/state`.** A proxied host's per-host settings are never fetched or
  written (the prefix never relays `/state`); they are the defaults.
* **Duplicates** are per mode: a proxied record is keyed on its peer id, so
  a direct and a proxied record of one broker are two ways in, not twins.

#### Relayed on the browser gate

| Method | Paths |
|---|---|
| GET | `/info`, `/sessions`, `/profiles`, `/profiles/config`, `/profiles/detect`, `/recordings`, `/recording`, `/recording/notes`, `/update/check` |
| POST | `/launch`, `/file/list`, `/file/read`, `/file/read_chunk`, `/file/hash`, `/file/write`, `/file/upload`, `/file/upload_begin`, `/file/upload_chunk`, `/file/upload_commit`, `/file/upload_abort`, `/file/paste_image`, `/file/delete`, `/file/mkdir`, `/file/copy`, `/file/move`, `/file/zip`, `/file/unzip`, `/file/stat`, `/file/setattr`, `/session/procs`, `/session/kill`, `/session/git`, `/session/mcp`, `/session/control`, `/recording/begin`, `/recording/chunk`, `/recording/commit`, `/recording/abort`, `/recording/delete`, `/recording/notes`, `/update/apply`, `/update/policy`, `/mods/policy`, `/profiles/config` |
| GET, PUT | `/mod-store/<modId>` |

#### Relayed on the MCP gate (`mcp: true` only)

| Method | Paths |
|---|---|
| GET | `/mcp/info`, `/mcp/terminals`, `/mcp/profiles`, `/mcp/desktop`, `/mcp/config` |
| POST | `/mcp/read`, `/mcp/input`, `/mcp/reset`, `/mcp/flush`, `/mcp/pace`, `/mcp/launch`, `/mcp/window`, `/mcp/desktop`, `/mcp/config`, `/mcp/knobs` |

**Anything absent is denied** (exact match, no prefixes). Never relayed:
`GET /`, `/help-corpus.json`, `/vendor/*`, `/mods/<id>/<gen>/*`,
`/mods/install|uninstall|rescan|installed`, `POST /restart`,
`WS /browserland`, `GET`/`PUT /state` and `/status/fetch` (`/ws` and
`/control` cross as [sockets](#sockets)). A path naming another `/peer/` hop is refused
(`peer_hop_refused`). `OPTIONS` is not routed on the prefix (405, never
dialed): the page is same-origin with this broker, so no preflight arises.
Sanic resolves `.` and `..` segments before the relay sees the path
(`/peer/b/./info` is `/peer/b/info`; `/peer/b/../info` is the peer named
`info`); the relay then decodes the rest once, and the **table's** path is
what is dialed, never the client's bytes. A body sent with a bodiless
method (`GET`) is read and dropped, never forwarded.

**What crosses.** Allowlists both ways. Upstream: `Content-Type`,
`Content-Length` (only when a body is sent), `Content-Encoding`, `Accept`,
exactly one
`X-Browserland-Scope`, `X-Webterm-Admin` only when the caller sent it and
only on the browser gate, `X-Browserland-Mcp-Admin` only on the MCP gate, and the one `Authorization` this broker sets (the
peer's token). The upstream client adds no headers of its own (no
`User-Agent`) and keeps no cookies. This broker's own bearer, the peer-token and pairing headers,
`Cookie`, `Host`, `Origin`, `X-Forwarded-*` and every hop-by-hop header never
cross, and `token` / `auth` are dropped from the forwarded query. Back:
`Content-Length`, `Content-Encoding`, `Content-Disposition`. `Set-Cookie`,
`Location`, every `Access-Control-*` and every cache header (`Cache-Control`,
`ETag`, `Last-Modified`, `Expires`, `Age`) are dropped, and every `/peer/*`
response is `Cache-Control: no-store`, so a peer cannot pin its answers in
the browser.

**A relayed body is never a page.** `Content-Type` is forced:
`application/json` when the peer said JSON, otherwise
`application/octet-stream` (a peer's `text/html` included), and **every**
`/peer/*` response — this broker's own refusals too — carries
`X-Content-Type-Options: nosniff` and
`Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'`.
A peer's script on this origin could read every host token the page holds.

**Loops, timeouts, load.** Redirects are never followed (a 3xx is relayed
without `Location`; a 1xx is never relayed). A peer's `broker_id` is
advisory only. The origin's host name is resolved when it is dialed, so name
a peer by an address, or by a name whose DNS you control. The "not this
broker" check catches a typo, not every alias of this machine; the hop
refusal is what stops a loop. Concurrent dials
on the browser gate are capped at **8 per peer within 32 overall**; the MCP
gate (whose callers this broker cannot authenticate) has its own pool of
**2 per peer within 8**, so it can never take a browser-gate slot, and it
reads each request body whole (at most 1 MiB, within 5 s) before it takes a
slot. Over either cap: 503 `peer_busy`, never a queue. A whole dial ends
after **600 s** however steadily the peer drips (504 `peer_timeout`, or a
dropped connection once the answer has started); a recording download
(`GET /recording`, up to 256 MiB) gets **3600 s**, enough at about
0.6 Mbit/s. `Range` and `If-None-Match` are not forwarded: the peer answers
a recording whole, with no partial answer and no ETag. Connect timeout 2.5 s (under the page's 3 s fetch deadline),
read timeout 30 s (past the 15 s `/mcp/read` wait and the 10 s desktop relay).
Bodies stream both ways, one chunk at a time — a 256 MiB recording is never
held whole; a body that dies half-way drops the connection rather than
arriving short. Nothing is retried. Log lines name the peer **id**, the
method, the table path and the error class — never the upstream URL or its
query (the `httpx` loggers are held at WARNING for that reason).

#### Sockets

A page cannot set headers on a WebSocket handshake, so a proxied host's
`/ws` and `/control` take a **one-time ticket** instead of a token.

1. **The mint.** `POST /peer/<id>/ws-ticket` with the body
   `{"route": "ws" | "control", "clientId": "<this page's client id>"}`
   (at most 4 KiB) goes through the browser gate above — the same
   credentials and the same refusals in the same order — and answers
   `{"ok": true, "ticket": "<43 characters>", "ttl": 30}`. A body that is
   not that is 400 `bad_ticket`; a `GET` is 405.
2. **The ticket** lives **30 s** and works **once**: its first presentation
   burns it whatever the outcome, so a ticket shown with the wrong peer,
   route or clientId is gone. It is bound to the peer, the route, the
   clientId, the peer's origin and this broker's token as they were at the
   mint. At most **16 per peer within 64 tickets** wait unredeemed; over
   either, 503 `peer_busy`. The page mints at most 4 tickets per host at a
   time, so a burst of re-dials (a restore, or every window after the peer
   restarted) waits its turn instead of hitting that cap.
3. **The socket.** `WS /peer/<id>/ws?session=…&clientId=…&ticket=…` (or
   `/control?clientId=…&ticket=…`) carries the ticket and nothing else. A
   bad, expired, reused, mis-bound or voided ticket closes it **4403**
   (this broker's own code; a 4401 on these routes only ever comes from
   the peer) after the upgrade, before anything is dialed. The page
   answers a 4403 by minting a new ticket once, at once, and a second
   4403 in a row backs off like any failed dial.
   A ticket is not bound to the browser's `Origin`: it is minted only with
   this broker's token in a header (never a cookie), so another site cannot
   mint one, it lives 30 s and works once, and anything outside a browser
   can send any `Origin` it likes.
4. **The upstream handshake.** This broker dials the peer's own `/ws` or
   `/control` with the page's query minus `ticket`, `token` and `auth`, and
   with the peer's token **only in the handshake's `Authorization`
   header** — never in a URI, never logged. The server-side handshake never
   follows a redirect (a 3xx fails the dial), reads no proxy from the
   environment, sends no `User-Agent` and negotiates no compression. A peer
   on a build before this one needs nothing new: it reads that header when
   there is no `?token=`.
5. **The relay.** One upstream socket per page socket. Messages cross
   unchanged both ways — text stays text, binary stays binary, nothing is
   parsed (each leg keeps its own framing and its own pings) — and each
   direction waits for its send before it reads again, with at most 2
   messages buffered from the peer, so a slow page slows the peer. Nothing reconnects here: the page's own reattach does, with a new
   ticket.

**Pools.** Relayed sockets have a pool of their own, apart from the dial
pools: **32 per peer within 128 sockets**. A ticket holds its slot from the
mint, so an upgrade never fails for the pool; the refusal is the mint's
503 `peer_busy`.

**Liveness.** A pair is never closed for being quiet: a terminal with no
output overnight stays connected. Each leg is pinged every 20 s and dropped
after 20 s without an answer — the peer's by this broker, the page's by the
server (as every socket of this broker is) — and a leg that drops ends the
pair.

**Revocation.** Every ticket mint and every redemption first closes each
pair, and voids each ticket, whose peer is gone, whose origin is no longer
the configured one, or that was opened under another token of this
broker's: the peer's leg gets 1000, then the page's 4403 `revoked`. Neither
changes at runtime today — a changed config takes effect on restart; a
future editor calls `apply_change` after it writes — so existing sockets
stay open until the next mint or redemption, or `apply_change`.

| Close code | Sent to | When |
|---|---|---|
| the peer's code and reason | the page | the peer closed with a code a close frame may carry |
| 1012 | the page | the peer vanished (no close frame), or its code is one no close frame may carry (1005, 1006, 2000, …) |
| 1011 `peer_unreachable` | the page | the dial at redemption failed (its slot is released) |
| 4403 `ticket` | the page | the ticket was bad, expired, reused, mis-bound or voided |
| 4403 `revoked` | the page (the peer gets 1000) | a peer, origin or token change unbound the pair |
| 4401 | the page | only ever the peer's own (it refused its token), passed through: the page asks for the peer's password |
| 1000 | the peer | the page went: its close, a lost connection, or a close this broker started for it (1009 for an oversized message, say) |
| 1001 | both | this broker is stopping (signalled before Sanic cancels the socket handlers, never guessed from who closed first) |
| 1011 | both | anything else (one log line naming the error class) |

Log lines name the peer id, the route, the first 8 characters of the
clientId and why a pair ended — never a ticket, a token, the upstream URL
or its query (the `websockets` logger is held at WARNING).

**Limits.** A peer checks its token at the handshake only, so a relayed
socket outlives a change of the peer's token until either side closes it,
and the peer's own sockets to other clients are not this broker's to
close. The peer
sees every proxied client as this broker (its logs lose the client
address).

## Linux deployment

**Install** (for running *and* testing):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,pyte]"
```

Zero-setup alternative: `launchers/run-broker.sh` / `run-agent.sh`
bootstrap a venv with the runtime deps on first run — fine for running,
but they don't install pytest, so use the editable install for testing.

**Config**: copy `broker_config.linux.example.json` to
`broker_config.json` (or point `$WEB_TERMINAL_CONFIG` at it). Default
profiles are `bash` (`bash -l`, the default) and `sh` (plain `sh` — the
escape hatch when login-shell rc noise is unwanted). A token is required on
every bind including loopback; set `auth_token` (or `$WEB_TERMINAL_TOKEN`)
to choose the value yourself, or let the broker mint one into
`webterm_token.json` and read it back with `--print-token`.

**systemd**: installable units live in `launchers/systemd/`
(`webterm-broker.service`, `webterm-agent.service`). Copy to
`/etc/systemd/system/`, edit `User=`, the `/opt/web_terminal` paths and
the token, then `systemctl daemon-reload && systemctl enable --now ...`.
Caveats: the `bash -l` profile sources the service user's login profile
(use the `sh` profile if that produces noise), and `/launch`-spawned
agents are session leaders that survive broker restarts — they
re-register within ~10 s of the broker coming back.

**Tests on Linux**: `python -m pytest tests -q` → the full suite passes;
the Windows-ConPTY e2e suites skip on Linux. This
includes a real-POSIX-PTY suite (`tests/test_linux_pty.py`) that was
written blind on Windows and verified against a real Linux PTY before
deploying.

## Protocol (reference)

Producer → broker: `hello` (required first frame; same `window_id`
replaces), `title`, `resized` as JSON text; raw ANSI output **and**
snapshots as unframed binary. Broker → producer: `input`, `resize` (reply
`resized`), `snapshot_please` (reply one binary full redraw). Browser
attach: broker sends `resized` *before* requesting the snapshot. Snapshots
start `ESC[0m ESC[2J ESC[H` — deliberately no `ESC c`/`ESC[3J`, since
already-attached browsers receive them too — and end with a DEC-mode
re-assert postamble (`?2004h/l` + `?1h/l` + mouse reporting, per the
agent's live sniffer; #138, #154) so a reloaded xterm.js recovers
bracketed-paste, application-cursor and mouse-tracking state. Mouse
reporting is two mutually-exclusive groups — tracking level
(`9/1000/1002/1003`) and extended encoding (`1006/1016`) — and within
each the postamble emits the **resets first and the set last**, because
xterm.js maps DECRST of *any* group member back to the group default, so
a trailing reset would undo the set. Without this, a repainting TUI's
`ESC[2J` reliably survives `raw.py`'s `_trim` while the mouse DECSETs it
emitted at startup do not, and every click in `lazygit`/`btop` dies at
the first browser reload. The browser→broker `paste` frame type is a
legacy alias for `input` (#138), kept only for tabs served before the UI
switched to xterm's `paste()`. Windows caveat: ConPTY never forwards an
app's `?2004h` request to the terminal side (verified live on Server
2022), so for a detected foreground Claude Code — which parses
`ESC[200~` regardless — the UI hand-brackets pastes when xterm's own
bracketed-paste mode is off (`pasteTextToTerm` in the window lifecycle
fragment).

Known limits: no scrollback replay, no alt-screen modeling in snapshots.
Mouse input *is* forwarded (xterm.js encodes reports onto `onData` like
any other key), but the sniffer inherits `altscreen.py`'s heuristic
limits — 8-bit CSI (`0x9b`) is not matched, and `1005`/`1015` encodings
are untracked because the vendored xterm.js logs both as unsupported.

### Terminal control (#238)

An outside client (first user: Notifier) can drive **one** terminal — its
input and its size — without taking the broker's lease, so the desktop keeps
every other terminal. It authenticates with the same browser token as `/ws`
(no new token class); `GET /info` carries `"terminal_control": 1` on a build
that enforces it. A client feature-detects on that integer.

Every surface carries one **view**: `{"state": "free"|"controlled", "label":
<str|null>, "since": <epoch ms|null>, "gen": <int>}` (`label`/`since` are
`null` when free). Within one broker run `gen` is strictly increasing per
terminal id — across a same-id producer reconnect too — so clients may order
views by it and drop any older than the last one they applied. Across a broker
restart it is seeded from the wall clock in milliseconds: that normally keeps
it increasing, but it is **not guaranteed** if the clock steps back, so a client
re-baselines (forgets its last `gen`) when the broker restarts.

* **Claim / release, on the controller's own `/ws`** (so the grant can never
  outlive the socket): attach as usual (`/ws?session=<id>&clientId=<own id>`,
  e.g. `ext-<uuid>`, never a browser's lease id), then send `{"type":
  "control", "op": "claim", "label": "<str>"}` or `{"type": "control", "op":
  "release"}`. A claim while another socket holds the terminal **takes over**.
  A claim from the holder relabels it at a new `gen`. The terminal is also
  released when the controller's socket closes, the terminal exits, or its
  producer drops or reconnects. A refused op answers `{"type": "error",
  "reason": "bad_label"|"not_controller"|"bad_op"|"unknown_session",
  "session_id": <id>}` and leaves the socket open.
* **Label**: 1–64 Unicode code points (a JS `length` can read up to twice
  that), shown to the owner verbatim (never normalized). It needs at least one
  visible character — a letter, number, punctuation or symbol other than the
  fillers U+115F, U+1160, U+3164, U+FFA0, U+2800, U+17B4, U+17B5 — so a label of
  only spaces, combining marks, joiners or variation selectors is refused.
  Refused anywhere: any control, surrogate, unassigned, line/paragraph-separator
  or format character, except ZWNJ/ZWJ (U+200C/U+200D) — so every bidi control
  (U+061C, U+200E–200F, U+202A–202E, U+2066–2069) and invisible ones like
  U+200B/U+FEFF are out. Categories come from the broker's Python Unicode
  database, so a code point newer than it counts as unassigned.
* **The gate**: while a controller holds a terminal, only that socket's input
  and resize reach the producer. Everyone else's, the lease holder's included,
  is dropped, exactly like a non-active socket's. With no controller the lease
  rule applies unchanged. A socket that lost the terminal (taken back, or
  another claim) watches, and its input is dropped even if its clientId holds
  the lease, until it claims again. A controller that dials with a browser's
  lease id is one of that browser's sockets: a `become_active` elsewhere closes
  it with 4409 like the rest, which releases.
* **What reaches the producer while controlled**: the controller's `input`,
  binary input and `resize`; legacy `paste` is gated like `input`, and mouse
  reports ride `input`. The `mouse` frame type stays a no-op. The snapshot
  request on every attach, MCP `/mcp/input`, and the `/session/*` and `/mcp/*`
  management RPCs (read, reset, flush, kill, pace) are **not** gated: MCP
  agents are not viewers (they can see `controlled` on `/mcp/terminals`).
* **Size**: only the controller resizes; the producer's `resized` echo goes to
  every viewer as always. Nothing is remembered or restored on release — the
  next driver's first `resize` decides.

**Telling everyone** (each change is published to the terminal's `/ws`
subscribers in `gen` order, each send bounded at 0.5 s, and every socket ends on
the live view; the `/control` pushes go out after, so two of them may arrive out
of order — order them by `gen`). A `/ws` or `/control` socket that cannot take a
frame within that bound is closed with 1012, so its page reconnects and re-reads
the state; a controller dropped that way loses control at once:

1. `/ws`, to every subscriber of that terminal: `{"type": "control", ...view}`
   on every change, and once on attach, right after `resized` and before the
   snapshot. The controller that lost the terminal first gets `{"type":
   "control", "state": "revoked", "by": "browser"|"claim", "gen": <new gen>}`.
2. `/control`, to every connected browser: `{"type": "terminal_control",
   "id": <id>, ...view}` (a controlled desktop window has closed its `/ws`, so
   this is how it hears of a release it did not cause). A terminal that exits
   while controlled still gets its `free` view on both surfaces, after `exit`
   and for an id that is already gone, so a page can clear its state.
3. `GET /sessions` rows: `"controller": {"label", "since", "gen"} | null`, so a
   page that boots or rebuilds mid-control re-enters the state.

**`POST /session/control`** (browser token) — body `{"id": <int>, "op":
"release", "gen": <int>, "clientId": "<lease id>"}` takes the terminal back
from its controller. The released controller gets `revoked` (`by: "browser"`)
and keeps its socket as a watcher. Answers, in the order they are checked:
`401 auth_required`; `400 bad_json` / `bad_id`; `404 unknown_session` (also for
a terminal that is exiting); `400 bad_op` (`op` is not `"release"`); `400
bad_gen` (not an integer ≥ 0; a boolean counts as bad); `409 {"ok": false,
"error": "not_active"}` when another browser holds the lease (`clientId`,
trimmed, is not the holder) — the same rule as `PUT /state`, so with no lease
holder at all the browser token alone passes; `409 {"ok": false,
"error": "stale_gen", "id", ...live view}` when the terminal is free or held at
another `gen` (a double click or a crossed claim never releases a newer
controller); `200 {"ok": true, "id", ...the new free view}`.

**The desktop's side (#239).** A desktop window whose terminal is controlled
holds **no** `/ws`: it closes its socket (handlers nulled), and
`attachWebSocket` refuses to dial while `win.controlled` is set, so it receives
nothing and sends nothing. `isResizable` refuses it too, so no tiling, drag or
browser resize sends a `resize`, and nothing is queued for later. The page keeps
one record per terminal (`<hostId>:<id>`, runtime only) fed by all four
surfaces:

* a `/ws` frame, a `/control` push or a `POST` answer is applied only when its
  `gen` is newer than the last one applied;
* a `/sessions` row is applied when that poll was sent after the record last
  changed, whatever its `gen`, which heals a missed push and a broker restart;
* a new `/control` socket to a host forgets that host's gens.

On attach, the `resized` that comes before the `control` frame is held until
that frame arrives; a `controlled` view drops it, so a window never snaps to
the controller's grid. Leaving the state clears the record first and then
reattaches, so the attach's forced initial `resize` sends this window's size. A
`409 not_active` from Take back shows the Become active prompt (home broker) or
masks that broker (remote), as a lost lease does.

### The /control lease channel (#248)

`WS /control` carries the single-active-browser lease for one browser profile
(`?clientId=<lease id>`). Frames the page sends: `{"type": "become_active"}`,
`{"type": "ping", "n": <int>}` and `desktop_result` (the answers to a relayed desktop op). Frames it gets:

* **`{"type": "status", "active": <bool>, "activeClientId": <str|null>, "gen":
  <int>, "bootId": "<str>"}`** — on connect, after `become_active`, on release,
  and wherever a status went out before. `gen` is the broker's lease generation:
  it bumps on every holder change and restarts with the broker, so a page keys
  "a new holding" on the pair (`bootId`, `gen`) — `bootId` names the broker run.
  A broker that predates #248 sends neither field.
* **`{"type": "pong", "n": <int|null>, "active", "activeClientId", "gen",
  "bootId"}`** — the answer to a ping, on the same socket. `n` echoes the ping's
  nonce (`null` if the ping carried no integer `n`); the rest is the live lease,
  built exactly like a status, so a page whose status went missing re-learns it
  from any ping, and the page runs it through the same path as a status frame.
* **A repeat `become_active` from the current holder is idempotent.** It bumps no
  generation, closes no socket (no 4409) and tells nobody else: only the sending
  socket gets the status again. A `become_active` from anyone else flips the
  lease, bumps `gen`, closes the previous holder's terminal sockets with 4409,
  and sends every `/control` socket its status.
* Every status or pong send is bounded at 0.5 s (`CONTROL_SEND_TIMEOUT`); a
  socket that cannot take one is closed with 1012 and its page reconnects, so
  one stalled browser never delays the rest. Releasing the lease closes nothing
  with 4409.

**Page-side liveness.** The page pings every 25 s with an increasing `n`. Only a
pong whose `n` matches an outstanding ping clears the **10 s pong deadline**, and
only a socket whose first status carried `gen` and `bootId` gets a deadline at
all, so an old broker is never declared dead. On a missed deadline the page
dials a replacement `/control` socket and keeps the old one working until the
replacement's first status arrives (make-before-break). A **5 s connect/status
watchdog** abandons a socket that has not delivered its first status that soon,
and restarts the 5 s once the socket opens. The home broker's channel down for
10 s shows the "lost contact with this broker — retrying…" notice.

**Terminal `/ws`: `not_active`.** When a browser that does not hold the lease
sends input, a paste, a binary frame or a resize on a terminal `/ws`, the broker
drops it (as before) and answers `{"type": "error", "reason": "not_active",
"session": <id>}`. It is advisory: sent at most once per socket per lease
generation, and never for a socket refused because of a terminal controller (see
Terminal control) or a revoked one. The page writes nothing to the terminal for
it; it asks the lease channel for the live state (a ping), which settles whether
this browser really lost the lease. `PUT /state` answers a non-holder `409
{"error": "not_active"}` as before.

## MCP HTTP interface

The broker exposes an **opt-in HTTP API** (the `/mcp/*` surface) that an MCP
server wraps as MCP tools so an AI agent can list, observe, drive, and launch
terminals. A ready-to-run server ships in [`webterm/mcptool/`](../webterm/mcptool/)
(stdio transport; `pip install -e ".[mcp]"`). The token-gated endpoints make
up the contract:

| Method | Path | Purpose |
|---|---|---|
| GET | `/mcp/info` | feature flags (`allow_launch`, `terminals` #253, `default_mode`, `"admin": <bool>` #245, `"window_edit": true` #243, `"allow_desktop": <bool>` #244) |
| GET | `/mcp/terminals` | list MCP-visible terminals |
| POST | `/mcp/read` | render a terminal's screen as plain text |
| POST | `/mcp/input` | type into a terminal (`readwrite` only) |
| POST | `/mcp/reset` | clear a terminal's screen-render buffer (`readwrite` only) |
| POST | `/mcp/flush` | discard a terminal's unread queued input (`readwrite` only) |
| POST | `/mcp/pace` | set a terminal's default send_keys pacing (`readwrite` only) |
| GET | `/mcp/profiles` | launchable profile names |
| POST | `/mcp/launch` | spawn a new terminal from a profile (every launch mints an ownership `handle`, #243) |
| POST | `/mcp/window` | set a window's `scope`/`mode` with its launch `handle`, or any window with the admin credential #245 (#243) |
| POST | `/mcp/knobs` | admin-only: set broker-wide knobs (`default_mode`/`allow_launch`/`allow_desktop`) #245 |
| GET | `/mcp/desktop` | read the lease-holding browser's desktop snapshot (#244) |
| POST | `/mcp/desktop` | relay one desktop op to the lease-holding browser (#244) |

The interface is **disabled by default** — nothing under `/mcp/*` answers until
an admin both enables it and sets a token (below). The broker serves the desktop
UI and this API, not this document, so editing these docs needs no restart.

### Enabling MCP & the token

MCP needs **both** the master `enabled` flag **and** a token. With either
missing every `/mcp/*` request is **403 `{"error":"mcp_disabled"}`**.

Get a token any of three ways (precedence **env > sidecar > config seed**):

* **Control Panel → MCP access → *Generate*** — mints a `token_urlsafe(32)`
  secret and persists it to the sidecar (below). The normal path.
* config `mcp_token` — a seed value in `broker_config.json`.
* env **`WEB_TERMINAL_MCP_TOKEN`** — **pins** the token over both of the above,
  is **never written to disk**, and disables UI token edits (the Control Panel
  shows `token_env_pinned: true`).

Pass it on every `/mcp/*` request as an `Authorization: Bearer <token>` header
or a `?token=<token>` / `?auth=<token>` query parameter (compared in constant
time). A missing/invalid token while the feature is enabled is **401
`{"error":"auth_required"}`**.

This MCP token is a **separate secret from the browser `auth_token`** and gates
**only** the `/mcp/*` data plane. Like every other surface it has **no
loopback exemption** (MCP is opt-in, so even a local caller needs the token).
CORS preflights exist for
browser callers; a server-side MCP client makes ordinary requests and is not
subject to CORS.

### Access modes

Each terminal has an **effective access mode** — its per-window override if set,
otherwise the broker-wide `default_mode`:

| Mode | Effect |
|---|---|
| `off` | hidden from `/mcp/terminals`; `/mcp/read` + `/mcp/input` → **404 `unknown_or_off`** (the same answer as a window outside the caller's declared scope) |
| `read` | visible + `/mcp/read`; `/mcp/input` → **403 `read_only`** |
| `readwrite` | read **and** `/mcp/input` |

* **`default_mode`** (global, default `off`) — applies to every window without an
  override; change it live in Control Panel → MCP access or `POST /mcp/config`.
* **per-window override** — set from the window title-bar **MCP access** menu or
  `POST /session/mcp`, where `"mode": null` clears it so the window inherits
  `default_mode` again. **Durable**: it is stored with the window's scope in
  `webterm_mcp_windows.json` (see the admin surface below) and re-applied
  whenever the same producer registers again, across an agent reconnect or a
  broker restart: same window id, same host, same nonzero process id. An agent
  relaunched under the same window id is a new process, so it starts back at
  `default_mode`, and so does a reconnecting producer that reports process id 0.
* **`allow_launch`** (global, default `false`) — independent flag gating
  `/mcp/launch` only.

### Scopes (`X-Browserland-Scope`)

A client may declare a **scope** on any `/mcp/*` request with the header
`X-Browserland-Scope: <name>` (#230). The name must fullmatch
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`: no `:` (window ids are `host:int`) and no
whitespace. A missing header or an empty value means **unscoped**. A value
outside the grammar, or the header sent more than once, is **400 `bad_scope`**
on every token route, checked after `mcp_disabled` and the token, so a caller
without the token cannot probe names.

* **Unscoped** sees every window its mode allows, tagged or not: the
  legacy/admin view.
* **Scoped `S`** sees only windows tagged `S`. `/mcp/terminals` omits the rest,
  and every id-taking route answers an out-of-scope window with the same
  **404 `unknown_or_off`** bytes as a missing one, so existence does not leak.
  An untagged window is invisible to every scoped caller.
* `/mcp/terminals` rows carry `scope` (the window's tag, `null` when untagged)
  and `/mcp/info` echoes the caller's declared `scope`. `/mcp/profiles` ignores
  the scope.
* CORS preflights allow the header unconditionally, and every `/mcp/*`
  response lists it in `Vary`.
* A window's tag is set by a scoped `/mcp/launch` (below) or by the
  browser-realm `POST /session/mcp` (admin surface), and stored with its mode
  override in `webterm_mcp_windows.json`, so both survive a broker restart.
* **`GET /info`** (browser token) carries `"mcp_scopes": true` on every broker
  that implements all of this: the partition, `/session/mcp`'s `scope`, and a
  durable scope and raw mode override. A client feature-detects on it; a broker
  without the key predates all three. The desktop shows its scope controls only
  for a broker that says so, and there it stops re-asserting its own synced copy
  of each window's mode: the broker's stored override is the one authority.
* A scope is a convention, not a credential: every client holds the same MCP
  token and may declare any scope, or none.

The shipped MCP server (`webterm.mcptool`) fails closed: before its first call to
a scoped host it reads `/mcp/info`, and it refuses every call to that host with
`scope_unsupported` when the `scope` echo is absent (an older broker) or differs
(a proxy dropped or rewrote the header).

### The MCP admin credential (`X-Browserland-Mcp-Admin`) — #245

One designated agent can manage the whole MCP surface on the owner's behalf.
Admin is **its own secret** — `mcp_admin_token`, minted in Control Panel → MCP
access (generate = mint/rotate, clear = remove), pinnable with env
**`WEB_TERMINAL_MCP_ADMIN_TOKEN`** (same rules as the token pin: env wins, the
value never reaches disk, UI edits are disabled) — and it is **unset by
default**, so admin is off out of the box. One shared admin token serves every
admin agent; it is separate from both the MCP token and the #191 browser
`admin_token` (an MCP admin gets no `ADMIN_ROUTES`).

**Wire.** The credential travels as the **`X-Browserland-Mcp-Admin`** header
**in addition to** the normal MCP token — never instead of it. A credential in
the URL query string is refused outright (400 `admin_token_in_url`, both keys
and values scanned in constant time, logged with the route and peer but never
the value), even when the header is also correct, following the #191 rule —
the broker keeps its own access log off, but a reverse proxy or tunnel in
front of it (nginx, `tailscale serve`) or the browser's history may record
full URLs, so a token in a query string can leak there: the header is the
only accepted channel. A **repeated** admin header is refused like a repeated
scope header (**403 `admin_required`** — fail closed rather than silently
picking one).
The refusal ladder on every token route is fixed order:
`mcp_disabled` → 401 `auth_required` → 400 `bad_scope` → 403 `admin_required`
(the header is present but wrong) → the route's own checks. A header sent
while no admin token is configured is **ignored** (that caller is a plain
non-admin, and `/mcp/info` answers `admin: false`); an admin-only operation
called without admin also answers 403 `admin_required`. CORS: the header name
is advertised **unconditionally** in `/mcp/*` preflights (the realm is
runtime-mutable, so a cached conditional answer would go stale for the 86400 s
`Max-Age`), and every `/mcp/*` response lists it in `Vary`.

**Reach.** An admin lists every window — `off` windows and foreign-scope
windows included, each row carrying its raw `scope` and effective `mode` —
and the out-of-scope `unknown_or_off` filter is skipped for it (reads and
input still require the window's own mode; mode gates terminal I/O, not
visibility). An admin may set any window's scope/mode (`POST /mcp/window`
without a launch handle, #243), arrange the desktop when `allow_desktop` is
on (#244), and set the broker-wide knobs (`POST /mcp/knobs`, below). An admin
that also declares a scope keeps all of this: the scope is ignored for
visibility and reach, and only tags the admin's own launches.

**Never.** The MCP token, the admin token and `enabled` stay browser-only —
`/mcp/knobs` answers 400 `bad_key` for them before any write.

### Desktop relay (#244)

`allow_desktop` (Control Panel → MCP access, `POST /mcp/config`, or an
admin's `POST /mcp/knobs`; default **off**) is the master switch for the whole
desktop relay — window arranging *and* workspace management, admin included.
`GET /mcp/info` advertises it as `"allow_desktop": true|false`. The two routes
below are gated by the full `/mcp/*` ladder and then by the flag (**403
`desktop_disabled`**), and they relay each op to the **browser holding the
control lease** over its `/control` socket: one frame per op
(`{"type":"desktop_rpc","rid":<int>,"op":…,"args":…,"gen":<int>,"bootId":<str>}`),
one answer per rid. `gen` is the lease generation the call was made under and
`bootId` the broker run; rids start at a random offset per run, and the
browser keys the answers it has given on host, run and rid (#250).

* **No lease holder** → **409 `no_active_browser`** (fail, never queue); a
  browser that does not answer within the 10 s end-to-end deadline → **504
  `desktop_timeout`**; a browser that lost the lease mid-op → **409
  `{"error":"lost_lease","dispatched":true|false}`**. Each op applies whole or
  not at all, but `dispatched: true` means a send of the op was started, so a
  browser may have it and may have applied it before it learned it was
  deposed: check with `get` before retrying. Only `dispatched: false` (no browser was ever sent the op)
  promises nothing was applied. The broker never sends an op once the lease
  has moved on, and the browser refuses an op from a host whose lease it lost,
  from an older lease generation, or from another broker run (#250).
* **Visibility.** Every frame relayed for a NON-admin caller carries
  `args.visible` — the int ids of the caller's visible windows on this broker
  (effective mode ≠ `off`, in scope), computed at relay time; an admin's frame
  carries `null`. The browser refuses `unknown_window` unless every window
  the op **affects** — the named window, plus the whole column for
  `move_column`/`set_width`, plus both windows' columns for `tab_into`/
  `split` — is in that set, and the broker re-checks every **named** id
  before relaying, answering the identical **404 `unknown_or_off`** bytes.
  `get` for a non-admin keeps only rows whose id is in the visible set (the
  broker re-filters rows against its own registry as a belt: identity and
  titles come from the registry, never from the browser). Workspace ops are
  **admin-only at the broker** (`403 admin_required` before any relay).
* **Ops.** Window ops: `get`, `focus`, `move_column` (`to` = `left`/`right`
  or an absolute column index — an index move expels a shared window to its
  own column first, like a drag), `set_width` (`preset` = `1/3`/`1/2`/`2/3`/
  `max`), `tile`, `float`, `set_geometry` (`x`,`y`,`w`,`h`, floating only,
  browser-clamped, answered with the **applied** box), `tab_into`
  (`target`), `split` (`target`, `axis` = `row`/`column`). Workspace ops
  (`workspaces:list`/`add`/`rename`/`remove`/`switch`/`send-window`) are
  forwarded to the mod's `ctx.commands`; the mod being off/absent answers
  **200 `{"ok": false, "reason": "inactive"|"absent"}`**, not an error.

**`GET /mcp/desktop`** — op `get`; answers `200 {"ok": true, …snapshot}`.
The snapshot (as the lease-holding browser produces it, after the broker's
registry re-identification) carries `viewport`, `mode`, `focused_column`,
`workspaces` + `active_workspace` (`null` with the mod off), `home` (whether
that browser's home is this broker), and one row per live window: `key`,
`id` (an int for this broker's windows, `null` for app/foreign windows),
`title`, `tiled`, `column`, `row`, `tabIndex`, `width_preset` (+`widthFrac`
for a custom width), `tabbed`/`split`, `minimized`, `workspace`, and
`geometry` (`{x,y,w,h}` when floating — **per-browser pixel geometry, as that
browser sees it**; never stored on the broker) or `null` when tiled.
Every top-level field is rebuilt and type-checked broker-side — `viewport`
ints, `mode` a short string, `focused_column`/`active_workspace` int or null,
`home` a bool, `workspaces` a bounded list of small typed rows or null — so
a lying or buggy browser cannot smuggle foreign ids or oversized strings
past the relay. Each workspace row is reduced to `{index, id, name}` with an
unnamed workspace appearing as `name: null` (only a present, non-string or
oversized name drops the row).

**`POST /mcp/desktop`** — body `{"op": …, "args": …}` with the op allowlist
above (else **400 `bad_op`**); op-specific arg errors answer **400
`bad_args`**. The browser's own error codes map as:

| browser `error` | HTTP | body |
|---|---|---|
| `unknown_window` | 404 | `unknown_or_off` — byte-identical to the chokepoint's 404 |
| `bad_args` / `bad_op` | 400 | `bad_args` / `bad_op` |
| `not_home` | 409 | workspace op arrived on a non-home broker's relay |
| `lost_lease` | 409 | `{"error":"lost_lease","dispatched":true}` — the browser lost the lease mid-op |
| `needs_confirm` | 409 | `{"error":"needs_confirm","count":<live windows>}` — pass `force: true` |
| `last_workspace` | 409 | the last workspace cannot be removed |
| `absent` / `inactive` | 200 | `{"ok": false, "reason": …}` — the workspaces mod is off/absent |
| `error` (op threw) | 502 | `desktop_op_failed` |

`force: true` on `workspaces:remove` moves the windows to the **adjacent**
workspace; **no dialog is ever shown in the browser for an MCP op**.

### Endpoints (MCP-token-gated)

All require a valid MCP token; `read`/`input` additionally require the target's
effective mode to permit them.

| Method · Path | Requires | Purpose |
|---|---|---|
| `GET /mcp/info` | token | feature flags |
| `GET /mcp/terminals` | token | visible terminals |
| `POST /mcp/read` | mode ≥ `read` | screen as text |
| `POST /mcp/input` | mode `readwrite` | send keystrokes |
| `POST /mcp/reset` | mode `readwrite` | clear the screen-render buffer |
| `POST /mcp/flush` | mode `readwrite` | discard unread queued input |
| `POST /mcp/pace` | mode `readwrite` | set the default send_keys pacing |
| `GET /mcp/profiles` | token | profile names |
| `POST /mcp/launch` | `allow_launch` | spawn a terminal |
| `POST /mcp/window` | handle or admin | set a window's scope/mode (#243) |
| `POST /mcp/knobs` | admin credential | broker-wide knobs (#245) |
| `GET /mcp/desktop` | `allow_desktop` | desktop snapshot (#244) |
| `POST /mcp/desktop` | `allow_desktop` | one desktop op (#244) |

**`GET /mcp/info`** →
`{"ok":true,"allow_launch":false,"terminals":true,"default_mode":"off","version":"0.8.0+ba4b62e",
"scope":null,"admin":false}`.
`terminals` (#253) is `false` on a broker that hosts no terminals: every launch
is refused with `terminals_disabled` whatever `allow_launch` says. Absent from an
older broker, which launches.
`version` is this broker's build id (`webterm.build_version()` — package version +
git short hash, or the bare package version off a checkout) for stale-deploy
detection (#22). `scope` is the caller's own declared scope echoed back: `null`
when it declared none or a proxy stripped the header, and absent from a broker
that predates scopes. A client that declared a scope checks it here to fail
closed (#230). `admin` (#245) is `true` only when this call carries a matching
`X-Browserland-Mcp-Admin` credential — absent from a pre-#245 broker, which
mcptool reads as "not admin" and fails closed on. `window_edit` (#243) and
`allow_desktop` (#244) advertise `POST /mcp/window` and the desktop relay the
same way; a flag absent from an older broker reads as false.

**`GET /mcp/terminals`** → an array; windows whose effective mode is `off` are
omitted, and so are windows outside the caller's declared scope. `agent` is the
detected foreground-agent name (`""` when none), `kind` the producer kind
(`"agent"` vs a non-agent `"terminal"`), `mode` the effective access mode,
`scope` the window's scope tag (`null` when untagged). `version` is the
producer's reported build id (`""` for a pre-#22 agent / a non-agent producer);
for **agent** producers a `stale` boolean flags a
build differing from this broker's (a deploy predating a fix — reliable when
builds carry a git hash). `app_cursor` is the cached DECCKM (application-cursor
mode) the MCP `send_keys` reads to pick CSI vs SS3 arrows (#23); `pace_ms` is the
window's default `send_keys` inter-key pacing (#133, `0` = single-burst, set via
`/mcp/pace`) the MCP server reads so a no-`delay_ms` send auto-paces. `pyte` (#134)
is whether the agent has pyte installed: `false` means its `read_screen` uses the
dependency-free textgrid fallback — no `attr_runs` (#128) and no keyframe repair
(#130), so a sparse alt-screen frame after ring eviction comes back flagged
`partial` only. It defaults `true` for a pre-#134 agent that omits the field.
`controlled` (#238) is whether an outside client is driving the terminal right
now (see Terminal control); MCP input is not gated by it:

```json
[{"id":4503603655475937,"title":"bash","host":"JC-SERVER","cwd":"/home/me",
  "agent":"","kind":"agent","cols":80,"rows":24,"mode":"read","scope":null,
  "version":"0.8.0+ba4b62e","stale":false,"app_cursor":false,"pace_ms":0,
  "controlled":false,"pyte":true}]
```

**`POST /mcp/read`** — body `{"id": <int>}` plus optional fields:
`view: "scrollback"` + `lines` reads scrollback history instead of the live
screen (`lines` is hard-capped at 1000, #21; the reply echoes `view` and
`history_lines`); `since` (a prior `content_hash`) asks for a delta reply —
the response always carries a `delta` boolean, with `changed_rows` present
only for a real delta (#52); `attrs: true` opts into an `attr_runs` styled-run
map (fg/bg/reverse) so a color-only change is observable (#128).

```json
{"ok":true,"id":4503603655475937,"cols":80,"rows":24,"text":"<screen lines>\n...","content_hash":"…","stable_hash":"…","idle_ms":0}
```

`content_hash` is a stable 128-bit digest of the rendered text (#26).
`stable_hash` (#135) is the same digest with the HARDWARE-cursor cell masked to a
sentinel, so a cursor BLINK in place (the cell toggling between the block glyph
and the character under it) leaves it unchanged while a cursor MOVE — which
exposes a different underlying cell — still changes it. It is always present
(empty string for a degraded read or an older agent) and equals `content_hash`
when there is no cursor.

`idle_ms` is a best-effort count of ms since the terminal last emitted PTY output
(a current agent always reports it — `0` means output landed just now; an older
agent omits it, so it reads as unknown rather than a misleading `0`). It is
unreliable for a perpetually-animating app that paints every frame (its `idle_ms`
never grows), so pacing/flush and a semantic content wait are the real settle
signals there, not `idle_ms`.

The read can also BLOCK for one bounded round-trip instead of returning
immediately, on any ONE of four mutually-exclusive wait signals (combining two →
**400 `conflicting_wait`**), all capped by `timeout_ms` (≤ 15000):
`wait_for_change` (a prior `content_hash` — wake when the screen differs, #26),
`wait_for_text` / `wait_for_regex` (+ `wait_absent` — wake when the grid contains
or no longer contains a match, #51), and `wait_for_idle` (a settle window in ms —
wake once `stable_hash` has held steady for that many ms, i.e. output went quiet,
#135). The predicate and idle waits set `matched` (true when satisfied, false on
timeout); the RPC timeout is stretched by `timeout_ms` so the agent's hold
outlives it. `wait_for_idle` settles only a CALMER TUI — a fully-animating app
(Dwarf Fortress animates creatures/water/the `*PAUSED*` marquee off the cursor
every frame) never reaches output-idle, so there use pacing/flush plus a semantic
`wait_for_text` check, not idle.

The agent renders the screen off its event loop. With pyte it returns the full
grid; without pyte it falls back to a dependency-free in-house emulator
(`agent/snapshot/textgrid.py`) that still produces a **bounded** `rows`×`cols`
grid — so a full-screen TUI reads as a clean grid (box-drawing/braille intact),
not an unbounded raw-ANSI dump (#15). Both paths are real grid renders, so
**`"degraded": true`** is now reserved for the rare last-ditch raw decode (it
no longer appears for ordinary TUIs). The textgrid fallback has no keyframe
repair (that is pyte-only, #130), so a sparse alt-screen frame after ring
eviction comes back flagged **`"partial": true`** (#134) — the grid is a real
render, just possibly incomplete; the per-terminal `pyte` flag on
`/mcp/terminals` tells a caller up front which path an agent is on. Only
**agent** producers answer; a non-agent terminal producer has no handler, so the
request times out → **502 `{"error":"no_producer_rpc"}`**.

**`POST /mcp/input`** — body `{"id": <int>, "data": "<str>"}` → `{"ok":true}`.
Requires effective mode `readwrite` (else **403 `read_only`**); `data` must be a
string (else **400 `bad_data`**) and ≤ **256 KiB** UTF-8 (else **413
`too_large`**). Forwarded **verbatim** straight to the PTY and **deliberately
bypasses the single-active-browser lease** — MCP is its own authorized channel.
It bypasses a terminal's external controller (#238) the same way.
The high-level MCP `send_input` *tool* maps newlines in `data` to a carriage
return before calling this endpoint, so a command submits on PowerShell/PSReadLine
(which treats a bare `\n` as a soft continuation) and on a Unix shell alike (#13);
the endpoint itself is byte-exact, so drive it directly for a literal LF or
raw-mode bytes.

**`POST /mcp/reset`** — body `{"id": <int>}` → `{"ok":true,"id":<int>}`. Requires
effective mode `readwrite` (else **403 `read_only`**). A correlated producer
round-trip (like `/mcp/read`): the agent clears its PTY-output ring so the next
`read_screen` renders from a clean slate, then acks. Only **agent** producers
answer; a non-agent producer times out → **502 `no_producer_rpc`** (and a rare
agent-side failure → **502 `reset_failed`**). It touches Browserland's render
buffer only — it sends nothing to the running app.

**`POST /mcp/flush`** — body `{"id": <int>}` → `{"ok":true,"id":<int>}`. Requires
effective mode `readwrite` (else **403 `read_only`**). The **input-side mirror**
of `/mcp/reset`: where reset clears the OUTPUT ring, this discards keystrokes
queued toward the app but not yet consumed (a runaway `send_keys` backlog a
frame-polling TUI hasn't drained), so the next `read_screen` reflects the settled
state. Same correlated round-trip — only **agent** producers answer, so a
non-agent producer times out → **502 `no_producer_rpc`** (a rare agent-side
failure → **502 `flush_failed`**). On a Windows/ConPTY agent it is a best-effort
no-op (that backend exposes no input-queue flush primitive) and still acks `ok`.

**`POST /mcp/pace`** — body `{"id": <int>, "pace_ms": <int>}` →
`{"ok":true,"id":<int>,"pace_ms":<clamped int>}`. Requires effective mode
`readwrite` (else **403 `read_only`**). Sets the window's **default** `send_keys`
inter-key pacing so a subsequent MCP `send_keys` that passes no `delay_ms`
auto-paces (one key per POST) — for a frame-polling raw-input TUI (Dwarf Fortress)
that drops a burst read faster than it renders. `pace_ms` must be an integer (else
**400 `bad_pace`**) and is **clamped** to `[0, 1000]` (`0` disables → single-burst;
an over-cap value pins to `1000`). Unlike `/mcp/reset`/`/mcp/flush` this is
**broker-local** with **no producer round-trip** — it just stamps the window's
in-memory `pace_ms`, which `/mcp/terminals` surfaces for the MCP server's
client-side pacer. The value is **ephemeral per-connection**: it resets whenever
the window registers again (agent reconnect or relaunch). Unlike a per-window
mode override, it never carries over a reconnect.

**`GET /mcp/profiles`** → `{"default":"cmd","profiles":["cmd","powershell"]}`
(the broker's configured `agent.profiles`; `bash`/`sh` on Linux). Profiles
are global: a declared scope never changes this list, and a scoped caller may
launch any of them.

**`POST /mcp/launch`** — refused with **403 `terminals_disabled`** when the
broker has `terminals: false` (#253, checked first, whatever `allow_launch` says),
then requires `allow_launch` (else **403 `launch_disabled`**).
Body reuses the `/launch` shape plus an MCP-only `mode`: `{"profile": <str>,
"cols": 80, "rows": 24, "title": <str>, "cwd": <str>,
"mode": "off"|"read"|"readwrite"}`, all optional (dims default 80×24, `profile`
defaults to the broker default; `cwd` must be an existing dir; an invalid
`mode` is **400 `bad_mode`**, scoped or not, and `null` counts as absent).
Response is the launcher's:

```json
{"ok":true,"id":4503603655475937,"registered":true,"agent_pid":12345}
```

**200** when the agent registered within 10 s, **202** when it spawned but had
not said `hello` yet (`"registered":false`).

Every **MCP** launch, scoped or not, takes the same path (#243): the window's
row in `webterm_mcp_windows.json` is written before the agent is spawned, with
a `null` pid and host, and the agent's first `hello` claims it — even a `hello`
that reaches a restarted broker after a 202. The window comes up in the body's
`mode`, or in **`readwrite`** when there is none (**not** `default_mode`,
scoped or unscoped alike: a launched worker is meant to be driven by its
launcher, and `allow_launch` is already its own opt-in). The broker also mints
a **launch handle** — `secrets.token_urlsafe(32)` — and stores only its SHA-256
hex digest as the row's `owner`; the plaintext handle is returned in the
response (`"handle": "…"`, alongside `"scope"` and `"mode"`; `"scope"` is
`null` for an unscoped launch) and nowhere else. An unscoped launch's row
survives on its `owner` (and mode) even with no scope. If the launch fails
after the row was written, the row is removed again — unless the agent's
`hello` already claimed it. A launch whose new window id already has a stored
row retries **once** on a fresh id; the collided row is deleted only when it
is a pre-spawn orphan (no pid and no host) or older than the 7-day prune age
— never a row a reconnecting producer could still claim, whose off/scope
policy stays untouched (the retry runs whether or not the row was deleted).
Only a row write that fails, or a retry whose fresh id collides again, is
**500 `prespawn_failed`**, and nothing is spawned. The browser's
`/launch` is unchanged: it never reads `mode` and writes no row.

**`POST /mcp/window`** — body `{"id": <int>, "handle"?: <str>, "scope"?:
<str>|null, "mode"?: "off"|"read"|"readwrite"|null}` (#243). Sets the scope
and/or MCP mode of one window; an absent key leaves its field, an explicit
`null` clears it (exactly `/session/mcp`'s body rules). Two paths, tried in
order:

* **handle** (preferred, admin included): the row must carry an `owner`, the
  caller's handle must hash to it (constant-time compare), and a live entry
  for the id must be the row's own producer. The handle bypasses the
  off/out-of-scope filter — an owner can set its window to `off`, or to a
  scope it does not hold, and undo it. A matching-handle write **keeps** the
  owner.
* **no handle**: requires the admin credential; the write targets any window
  with a live entry or a stored row, and **clears** the old owner (a stale
  agent handle cannot undo an admin decision) while stamping the window as
  admin-driven (the taskbar badge).

Every handle-path failure — a wrong or missing handle, an unknown id, an
unowned row, a foreign producer — answers the **identical 404
`unknown_or_off` bytes** as every other id-taking route, so the route cannot
be used to probe which ids exist. Validation (`bad_json`/`bad_id`, `bad_mode`,
`bad_scope` — 400) runs before any write, and the authorization is re-checked
**inside** the store's single locked write, so an admin's owner-clear landing
between validation and commit turns a stale handle into the same 404 rather
than a write. The change persists across a broker restart and is re-applied on
the same producer's reconnect; a `/session/mcp` edit clears the row's `owner`,
after which the old handle gets the 404; a pre-spawn row (202 window before
its first `hello`) is editable by its owner. `GET /mcp/info` answers
`"window_edit": true` on a broker that has this route.

### Error reference

| Status | `error` | When |
|---|---|---|
| 403 | `mcp_disabled` | feature disabled or no token configured |
| 401 | `auth_required` | missing/invalid MCP token |
| 400 | `bad_scope` | `X-Browserland-Scope` is not a valid scope name, or is sent more than once (so a proxy that adds its own copy makes every call a 400); every token route, checked after mcp_disabled and the token. An empty value is not refused: it means unscoped, which sees every window its mode allows |
| 400 | `bad_json` | body is not a JSON object |
| 400 | `bad_id` | `id` missing or not an integer |
| 404 | `unknown_or_off` | no such window, or its effective mode is `off`, or it is outside the caller's declared scope; on `POST /mcp/window` (#243) every handle-path failure answers these same bytes, so ids cannot be probed |
| 400 | `bad_data` | `/mcp/input` `data` is not a string |
| 403 | `read_only` | `/mcp/input` on a window not in `readwrite` |
| 413 | `too_large` | `/mcp/input` payload > 256 KiB |
| 400 | `bad_pace` | `/mcp/pace` `pace_ms` missing or not an integer |
| 502 | `no_producer_rpc` | `/mcp/read` · `/mcp/reset` · `/mcp/flush` producer did not answer (non-agent / timeout) |
| 502 | `reset_failed` | `/mcp/reset` agent could not clear its render buffer |
| 502 | `flush_failed` | `/mcp/flush` agent could not flush its pending input |
| 403 | `terminals_disabled` | `/mcp/launch` on a broker with `terminals: false` (#253); checked before `launch_disabled` |
| 403 | `launch_disabled` | `/mcp/launch` with `allow_launch:false` |
| 400 | `unknown_profile` | `/mcp/launch` profile not in config |
| 400 | `bad_dims` / `bad_cwd` / `cwd_not_dir` | `/mcp/launch` bad `cols`/`rows`/`cwd` |
| 429 | `too_many_pending_launches` | `/mcp/launch` backpressure |
| 400 | `bad_mode` | `/mcp/launch` `mode` is not `off`/`read`/`readwrite` (or `null`) |
| 500 | `spawn_failed` / `agent_exited_early` | `/mcp/launch` agent failed to start |
| 500 | `prespawn_failed` | `/mcp/launch`: the new window's row could not be written, or the one retry's fresh id also had a stored row; nothing was spawned |
| 400 | `admin_token_in_url` | an MCP admin credential was sent in the URL query string (#245) — the header is the only accepted channel, and the refusal holds even when the header is also correct |
| 403 | `admin_required` | the `X-Browserland-Mcp-Admin` header is present but wrong, or an admin-only operation was called without admin (#245) |
| 400 | `bad_key` | `POST /mcp/knobs` carries a key outside its three knobs — `token`, `admin_token` and `enabled` are browser-only (#245) |
| 400 | `bad_value` | `POST /mcp/knobs` boolean knob is not a real JSON boolean (`"false"` is refused, not coerced) |
| 403 | `desktop_disabled` | the desktop relay's `allow_desktop` switch is off (#244) |
| 409 | `no_active_browser` | `/mcp/desktop` with no browser holding the control lease (ops are never queued) |
| 504 | `desktop_timeout` | the lease-holding browser did not answer the desktop op in time |
| 409 | `not_home` | a `workspaces:*` op arrived on a relay whose lease holder's home is another broker |
| 409 | `lost_lease` | the browser lost the control lease mid-op. `dispatched: false`: the op was never sent, so nothing was applied; `dispatched: true`: it may have applied, so check before retrying (#250) |
| 409 | `needs_confirm` | `workspaces:remove` of a non-empty workspace without `force: true` — the body carries the live window `count` |
| 409 | `last_workspace` | the last workspace cannot be removed |
| 409 | `peer_origin_changed` | through a broker's `/peer/<id>/mcp` prefix: the mcptool host's `peer_origin` is not the address that broker now dials for the peer (#252) |
| 502 | `desktop_op_failed` | the browser's desktop op raised |
| 400 | `bad_op` / `bad_args` | `/mcp/desktop` op outside the allowlist / op arguments invalid |

### Admin surface (browser `auth_token`-gated)

These configure MCP and are **not** part of the MCP token's surface. They use
the browser **`auth_token`** (the same mandatory-token gate as `/state` and
`/file/*`): always required, on every interface, as `Authorization: Bearer` or
`?token=`.

**`GET /mcp/config`** →
`{"ok":true,"enabled":false,"token":"","admin_token":"","admin_token_env_pinned":false,
"default_mode":"off","allow_launch":false,"allow_desktop":false,
"token_env_pinned":false,"known_scopes":[],"python":"/usr/bin/python3",
"pythonpath":"/opt/browserland","local_url":"http://127.0.0.1:4445"}` (`token`
and `admin_token` are the live secrets, `""` when unset). `admin_token` (#245)
is the MCP admin credential, shown to the browser-token holder like `token`
(the browser is the root of trust); `admin_token_env_pinned` mirrors
`token_env_pinned` for it — `WEB_TERMINAL_MCP_ADMIN_TOKEN` pins the admin
token (env wins over the sidecar, the value never reaches disk, and the
Control Panel's admin controls are disabled). `allow_desktop` (#244) is the
desktop relay's master switch — a browser checkbox and an admin knob.
`known_scopes` lists the scopes on live
windows (any mode) and on stored per-window rows, sorted; a name a caller only
declared on the header never appears. `python` is the interpreter running this
broker (`sys.executable`), `pythonpath` the directory holding the `webterm`
package it runs, and `local_url` a loopback URL on the port this request
arrived on (the configured port when there is no socket to read); the Control
Panel's **Copy .mcp.json** builds a client file from them (#235). They are
absolute server paths, shown only to a holder of the browser token.

**`POST /mcp/config`** — partial update of any of `enabled`, `default_mode`
(`off`/`read`/`readwrite`), `allow_launch`, `allow_desktop` (#244), `token`,
`generate:true` (mint a
fresh `token_urlsafe(32)`), `admin_token` (the #245 credential: `null` or `""`
clears it) or `admin_generate:true` (mint/rotate — a server-minted value only,
never a client-chosen secret). Validated before any write; returns the GET
shape. While the env pins the token, `token`/`generate` edits are **ignored**
(the live token stays the env value), and the same rule holds for
`admin_token`/`admin_generate` under `WEB_TERMINAL_MCP_ADMIN_TOKEN`. Errors:
`bad_json` (400), `bad_mode` (400),
`bad_token` (400, non-string `token`/`admin_token`).

**`POST /mcp/knobs`** (#245, admin credential required) — body
`{"default_mode"?: "off"|"read"|"readwrite", "allow_launch"?: bool,
"allow_desktop"?: bool}` →
`{"ok":true,"default_mode":…,"allow_launch":…,"allow_desktop":…}`.
The admin agent's broker-wide
knobs, validated before any write: an unknown key — in particular `token`,
`admin_token` or `enabled`, which stay browser-only — is **400 `bad_key`**, and
the booleans must be real JSON booleans (a string `"false"` is truthy junk →
**400 `bad_value`**, not coerced: the browser realm's `/mcp/config` keeps its
historical coercion as pinned wire). A non-admin call is **403
`admin_required`** before any validation. The write shares `/mcp/config`'s
locked sidecar writer, so the two routes never interleave.

**`POST /session/mcp`** — body `{"id": <int>, "mode": "off"|"read"|"readwrite"|null,
"scope": <str>|null}`, both keys optional →
`{"ok":true,"id":<int>,"mode":<str|null>,"scope":<str|null>}`. Sets the
window's per-window mode override, its scope, or both; a key left out leaves
that field as it was. `"mode": null` clears the override (the window inherits
`default_mode`); `"scope": null` or `""` untags the window; a scope name matches
`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. The response carries the stored values: `mode`
is the RAW override (`null` while inheriting), never the effective mode. Every
change is written to `webterm_mcp_windows.json` before it takes effect, so it
survives a broker restart (see Access modes), and an identical repeat writes
nothing. A browser-side edit also clears the row's `owner` (#243) — the human
decision wins over the launching agent's handle, which stops working at once.
Errors, in the order they are checked: `bad_json` / `bad_id` (400),
`unknown_session` (404, no live window with that id; nothing is stored),
`bad_mode` (400, an invalid mode, or neither `mode` nor `scope` given),
`bad_scope` (400), `write_failed` (500, the store could not be written; nothing
changed).

**Sidecar `webterm_mcp.json`** — the durable MCP config, written atomically next
to the `/state` store (default `<state_path dir>/webterm_mcp.json`; override with
config `mcp_state_path`). Schema:

```json
{"token": "<secret or null>", "admin_token": "<secret or null>",
 "default_mode": "off", "allow_launch": false,
 "allow_desktop": false, "enabled": true}
```

`token` is `null` when the env pins it (the secret stays off disk), and so is
`admin_token` under its own pin; `allow_desktop` (#244) is the desktop relay's
master switch, default off. The file self-heals if hand-edited or
truncated. It holds only these broker-wide knobs: per-window modes and scopes
live in `webterm_mcp_windows.json`, below.

**Sidecar `webterm_mcp_windows.json`** — the durable per-window MCP store,
written atomically next to the `/state` store (default
`<state_path dir>/webterm_mcp_windows.json`; override with config
`mcp_windows_path`). One row per window id:

```json
{"windows": {"<window id>": {"scope": "<str or null>", "mode": "<str or null>",
                             "owner": "<64-hex sha256 or null>",
                             "admin_set": false,
                             "pid": 12345, "host": "<str or null>",
                             "seen": 1790000000}}}
```

`mode` is the RAW override (`null` = inherit `default_mode`), `owner` (#243)
is the SHA-256 hex digest of a launch handle (never the handle itself), and
`admin_set` (#245) marks a window an admin credential launched or last
re-tagged (it feeds the taskbar's `mcp_admin` flag on `/sessions`). A row
carrying none of scope, mode or owner is deleted. `pid` (an int or `null`) and
`host` name
the producer the row was written for. Window ids are reused (an OS window
handle is recycled), so a row is re-applied only to a hello from the same host
with the same nonzero pid; a `null` pid or host is a wildcard, claimed by the
first hello with a nonzero pid that matches the rest (an `/mcp/launch`
writes its row that way, before the agent exists). `seen` (epoch seconds)
moves whenever a hello re-applies the row or a write changes it, and once a day
while the window stays connected; the hello bumps reach disk through a
coalesced writer, at most once a minute. Rows are never pruned at load, nor
during the first 10 minutes after the broker starts (every window gets that
long to reconnect). After that, each write drops rows whose window is not
connected and that were not seen for 7 days, then, while more than 1000 rows
remain, the oldest rows whose window is not connected (a connected window's
row is never dropped). A missing, unreadable or wrong-shaped file starts the
store empty and a malformed row is dropped, both logged; the next change
rewrites the file.

**Precedence.** Token: env `WEB_TERMINAL_MCP_TOKEN` > sidecar `token` > config
`mcp_token`. Effective mode: per-window override > global `default_mode`.

### Launch-profile editor (browser `auth_token`-gated) — #70

The Control Panel edits the launch-profile allow-list here. Same mandatory
browser-token gate as `/mcp/config` above — **never** the MCP token, so the
commands (the RCE-by-design half of profiles-only) only ever travel to an
already-authenticated browser. `/profiles` and `/mcp/profiles` stay **names
only**, so an MCP/AI agent still can't read a command or define a profile.

**`GET /profiles/config`** → the **full** objects for this host:
`{"ok":true,"default_profile":"cmd","profiles":{"cmd":{"command":[...],"title":...,
"cwd":...}},"os":"windows|posix","source":"config|sidecar","exists":{"cmd":true}}`.
`exists[name]` is `shutil.which(command[0]) is not None` (a red flag for a
shell that isn't installed).

**`POST /profiles/config`** — **replace** semantics; body
`{"profiles":{...},"default_profile":"..."}`. Validated in full **before** any
write (a bad field changes nothing), then written to the sidecar atomically and
the live launcher is swapped — **no restart**. Returns the GET shape. Errors
(all `400` unless noted): `too_large` (413, body > 256 KiB), `bad_json`,
`bad_profiles` (not a dict), `no_profiles` (empty — would brick `/launch`),
`too_many_profiles` (> 200), `bad_name` (empty / > 64 chars / control chars /
outside `[A-Za-z0-9 ._+-]`), `bad_profile`, `bad_command` (not a non-empty list
of non-empty control-char-free strings), `command_too_long`,
`command_token_too_long`, `bad_title`/`title_too_long`, `bad_cwd`/`cwd_too_long`,
`default_not_member`. An empty `default_profile` resolves to the first profile.

**`GET /profiles/detect`** → read-only environment scan seeding the editor:
`{"ok":true,"suggestions":[{"name","title","command","exists"}]}`. Windows lists
WSL distros (`wsl.exe -l -q`); POSIX lists installed `bash`/`zsh`/`fish`/`sh`.
Never errors on a missing tool — the list is just empty. Runs off the event loop.

**Sidecar `webterm_profiles.json`** — the durable profile set, written atomically
next to the `/state` store (default `<state_path dir>/webterm_profiles.json`;
override with config `profiles_state_path`). Schema:

```json
{"profiles": {"<name>": {"command": ["..."], "title": null, "cwd": null}},
 "default_profile": "<name>"}
```

Seeded from `broker_config.json`'s `agent.profiles`; **once written it owns the
set** (`agent.profiles` becomes seed-only). Self-heals — a missing/corrupt/empty
sidecar falls back to the seed, never bricking startup or `/launch`. Full recipe
catalog + the security rationale: **[[Launch-Profiles]]**.

### Mods (browser `auth_token`-gated) — #157 / #163 / #172

The desktop's mod system. Authoring contract, the `ctx` API and the trust model:
**[[Writing-a-Mod]]**. All of these are **`serve_ui`-gated** — a node
(`serve_ui: false`) registers none of them and reports `mods: []` — and, like
`/mcp/config`, they take the **browser** token, never the MCP one. They are
deliberately **not** lease-gated: this is broker configuration, and the point is
being able to administer a broker somebody else is using right now.

**A mod is not sandboxed.** It runs same-origin with the page's full authority
and can reach `/file/*`, `/session/*`, `/state` and `/mcp/*` directly; `ctx`
grants it nothing it lacks. So the trust event is the **token-gated install**,
not the fetch that follows.

**`POST /mods/policy`** — this broker's mod *pins*, `{modId: bool}`, applied to
every browser that loads its page (an absent id leaves the choice to that
browser). Stored in the `webterm_mod_policy.json` sidecar beside the `/state`
store (override with config `mod_policy_path`); reported by `GET /info` as
`mod_policy`. Applies on the next page load.

**`POST /mods/install`** — body
`{"manifest": {…}, "files": {"<name>": "<text>"}, "replace": false}`, ≤ 2 MiB,
validated **entirely in memory** before a byte is written. The mod id must match
`[a-z0-9][a-z0-9-]{0,63}` **and start with `x-`** (`reserved_id` otherwise — the
un-prefixed namespace is reserved for shipped mods). `manifest.scripts` is a
required non-empty list of `.js` names; `entry` and every other unknown key are
**rejected**, not ignored. Files are text only (`.js`/`.css`/`.md`), UTF-8,
BOM-free, newline-terminated, ≤ `ui._MAX_LINES` lines; a `.css` may not
`@import` or reference an absolute `url(http…)`. Caps: 32 mods, 32 files/mod,
256 KiB/file, 512 KiB/mod. Returns
`{"ok":true,"id","gen","replaced","adopts_existing_state":{"mod_store","pin"},
"mod":{…},"applies":"next_page_load"}`. Errors (each distinct): `too_large`
(413), `bad_json`, `bad_mod_id`, `bad_generation`, `reserved_id`, `id_in_use`
(409), `bad_file_name`, `reserved_file_name`, `too_many_files`, `file_too_large`
(413), `total_too_large` (413), `bad_encoding`, `bad_scripts`, `bad_styles`,
`bad_requires`, `bad_manifest_field`, `unknown_manifest_key`,
`css_external_reference`, `too_many_mods` (409), `write_failed` (500).

**`POST /mods/uninstall`** — `{"id": "x-notes", "purge": false}`. `purge:true`
also clears this broker's `/mod-store/<id>` and its pin (lock order
`mods_install → mod_policy → modstore`, data-first/code-last); it cannot touch
what other browsers hold in `localStorage`. **Not idempotent at the HTTP level**:
a retry after a lost response answers `404 not_installed` even though the first
attempt succeeded — treat that as possible success.

**`POST /mods/rescan`** — re-read `mods_dir` and sweep litter. An operator
convenience, **not a trust boundary** (anyone who can write `mods_dir` can write
the broker's source tree); the scanner refuses symlinks/reparse points, refuses
anything whose realpath leaves `mods_dir`, refuses a non-`x-` directory, and
validates every byte under the same rules an install obeys. The destructive
sweep refuses to run in a directory without the `.browserland-mods` marker.

**`GET /mods/installed`** — the operator detail `/info` leaves out: `mods_dir`,
per-file sizes and sha256s, `installed_at`, declared `tiers`, `has_help`, the
effective `limits`, and **what was skipped and why**.

**`GET /mods/<modId>/<gen>/<name>`** — one file of one generation of one
installed mod. **Public**, and forced rather than chosen: a `<script src>`
cannot carry an `Authorization` header, `?token=` is structurally banned (#144),
and a `fetch`+`blob:` workaround would need `blob:` in `script-src`. Same
posture as `GET /`, which already carries every shipped mod's source. Served
from an in-memory allowlist dict keyed `"<id>/<gen>/<name>"` — a client-supplied
segment can only ever hit a known key, so traversal is unrepresentable rather
than defended against, and no blocking IO reaches the event loop. Only
`.js`/`.css` are servable. `Cache-Control: public, max-age=31536000, immutable`
is honest because `<gen>` is a content hash. **Installed source and styles are
therefore readable by anyone who knows the id, the generation and the file name
— do not put a secret in a mod.** `help.md` is not servable here at all
(`content_type` returns `None` for it); its only surface is
`/help-corpus.json`, which since #173 withholds the installed sections — and so
the installed ids — from a caller with no token.

**`<gen>` is not a second secret.** `compute_gen` is sha256 over the canonical
manifest plus the sorted `(name, sha256)` pairs — no salt, no broker id, no
install timestamp — so anyone holding the exact bytes of a distributed package
recomputes the gen this broker stored, and a `200` here rather than a `404`
confirms it is installed. That is a **confirmation oracle over a candidate set
the caller already has**, not enumeration: a mod whose bytes were never
published stays unguessable in both segments, and what leaks about a public one
is one bit about code this route serves in full anyway. #173 is therefore
partial *by construction* — it stops the corpus **listing** installed ids, it
cannot hide a publicly distributed mod from someone who asks for it by name.
Salting the gen would close it and is deliberately not done: a generation's
directory name must content-address its own bytes (the scanner refuses one that
does not), which is what makes reinstalling identical bytes a no-op, keeps two
brokers' URLs for one package agreeing, and makes `immutable` honest.

**Store `mods_dir`** — runtime-installed mods live in a directory beside the
`/state` store (default `<state dir>/webterm_mods`; override with config
`mods_dir`), deliberately **not** `webterm/broker/mods/`, whose drift guard is
bidirectional. Layout `<id>/CURRENT` (the atomic `{"gen": …}` commit pointer) +
`<id>/<gen>/` holding `mod.json`, the mod's files and broker-written
`.gen.json`. `gen` is a sha256 over the canonical manifest plus the sorted
`(name, sha256)` pairs; **two generations are retained**, so a page that started
booting against one cannot be handed a file from the next mid-flight.

**Install/uninstall applies on the NEXT PAGE LOAD, never live**, and that is
forced: global lexical bindings cannot be removed, so re-executing a mod script
in the same page dies with `SyntaxError: Identifier … has already been
declared`. `?nomods=1` on the desktop URL boots with no mod fetched or
initialized at all — the rescue hatch when an installed mod makes the Control
Panel unreachable.

### curl

```bash
B=http://127.0.0.1:4445

# Admin (add -H "Authorization: Bearer $AUTH" — a token is always required):
# enable MCP, mint a token, default new windows to read, allow launching.
curl -s -X POST $B/mcp/config -H 'content-type: application/json' \
  -d '{"enabled":true,"generate":true,"default_mode":"read","allow_launch":true}'
# -> {"ok":true,"enabled":true,"token":"abc123...","default_mode":"read",...}
TOK='abc123...'   # the token from above

curl -s $B/mcp/info      -H "Authorization: Bearer $TOK"
curl -s $B/mcp/terminals -H "Authorization: Bearer $TOK"
curl -s $B/mcp/profiles  -H "Authorization: Bearer $TOK"

# Read a terminal's screen (use an id from /mcp/terminals):
curl -s -X POST $B/mcp/read -H "Authorization: Bearer $TOK" \
  -H 'content-type: application/json' -d '{"id":4503603655475937}'

# Promote that window to readwrite (admin), then type into it:
curl -s -X POST $B/session/mcp -H 'content-type: application/json' \
  -d '{"id":4503603655475937,"mode":"readwrite"}'
curl -s -X POST $B/mcp/input -H "Authorization: Bearer $TOK" \
  -H 'content-type: application/json' -d '{"id":4503603655475937,"data":"ls\n"}'

# Launch a new terminal from a profile:
curl -s -X POST $B/mcp/launch -H "Authorization: Bearer $TOK" \
  -H 'content-type: application/json' -d '{"profile":"bash","cols":100,"rows":30}'
```

### The shipped MCP server

A ready-to-run MCP server lives in [`webterm/mcptool/`](../webterm/mcptool/) — a
thin stdio wrapper that maps each endpoint above to an MCP tool (`mcp_info`,
`list_terminals`, `list_profiles`, `read_screen`, `send_input`, `send_keys`,
`set_pace`, `reset_terminal`, `flush_input`, `launch_terminal`). It connects to
the broker over HTTP and authenticates with the MCP token.

```bash
pip install -e ".[mcp]"                      # needs Python >=3.10 (the mcp SDK)
claude mcp add browserland --env BROWSERLAND_MCP_TOKEN=… -- python -m webterm.mcptool
```

See [`webterm/mcptool/README.md`](../webterm/mcptool/README.md) for config (env/flags), the
tool list, and how access modes / `allow_launch` still govern behavior.

## Tests

```powershell
python -m pytest tests -q
```

The full suite passes (the platform PTY e2e suites skip on the other
OS): protocol shapes, ring eviction, OSC sniffer split at every byte
index, raw/pyte snapshot rendering, agent↔fake-broker integration
(reconnect, snapshot ordering, title re-hello), real-ConPTY round trip
on Windows, real-POSIX-PTY round trip on Linux (bash
echo/resize/`stty size`/OSC title/exit code, Ctrl-C via TIOCSCTTY,
huge-paste backpressure), broker e2e as a subprocess (auth gates, CORS on
error paths and preflights, sanic-ext neutralized, relay invariants,
`/launch` → detached agent on both platforms), the mandatory-token policy
(token bootstrap/mint/adopt, a router-enumerating guard that every non-public
route answers 401 `auth_required`, and the WS 4401 gates on loopback),
non-loopback auth negatives + CORS positives via the box's LAN IP, plus the
newer surfaces: the MCP tools (read/reset/flush/pace),
the file API, session recording, the status-fetch proxy, the textgrid
fallback renderer, and UI asset assembly.

`pyte` is optional: `pip install pyte`. It is required only for
`--snapshot-mode pyte` (`raw` starts without it), but installing it also
upgrades MCP `read_screen` regardless of snapshot mode — attr_runs (#128)
and keyframe repair (#130); without it the dependency-free textgrid
fallback is used and a sparse alt-screen frame after ring eviction comes
back flagged `partial` (#134). Windows agents need `pywinpty>=2`.

## Relationship to xterm-py

This broker's relay and registry were adapted from `xterm-py`
(<https://github.com/JohnConnorNPC/xterm-py>), a separate
relay/registry/UI codebase, which is not part of or modified by this
project. This broker defaults to port **4445**.
