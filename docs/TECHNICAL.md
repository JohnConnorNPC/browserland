# Browserland (codename `webterm`)

**Browserland** is a self-contained web terminal system: **headless PTY agents** + a
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
python -m webterm.broker [--host 127.0.0.1] [--port 4445] [--config PATH] [--headless]
```

`--headless` (or config `"serve_ui": false`, default `true`) serves the full
JSON/WS API but **not** the desktop page or the in-app Help — `GET /` returns
`200 {"ui": false}`, `/help-corpus.json` 404s, and both UI constants
(`INDEX_HTML`, `HELP_CORPUS`) are never assembled (#87). `--headless` overrides
the config key (like `--host`/`--port`); there is no `--no-headless`. `GET /info`
reports the active mode as `serve_ui` (the one additive change to a JSON route);
existing JSON/WS behavior is otherwise unchanged in either mode.

Config (`broker_config.json`, path overridable via `$WEB_TERMINAL_CONFIG`;
see `broker_config.example.json`): `auth_token`, plus `agent.profiles` for
`/launch`. **Profiles only** — `/launch` accepts
`{"profile": "cmd", "cols": 120, "rows": 32, "title": "..."}` and never a
client-supplied command/cwd/env. Responses: `200` registered, `202` spawned
but no hello within 10 s, `400` unknown profile, `401`/`403` auth, `429`
too many pending, `500` agent exited early.

### Auth model

**A token is required on every surface, on every interface, always** (#142) —
no loopback exemption, no opt-out. Resolution (never returns nothing):
`$WEB_TERMINAL_TOKEN` → config `auth_token` → the `webterm_token.json` sidecar
beside the state store → **mint** `secrets.token_urlsafe(32)` into it. Compared
with `hmac.compare_digest`. `python -m webterm.broker --print-token` reports it
without minting or starting a server.

Loopback was never a sound exemption: `tailscale serve` in front of a
`127.0.0.1` bind (the topology [SETUP.md](SETUP.md) recommends) makes every
tailnet request arrive *from* loopback, and any page in the user's browser
reaches loopback too. Upgrading a tokenless install is a **breaking change** —
see [UPGRADING.md](UPGRADING.md).

**How the token travels (#144).** HTTP sends it as `Authorization: Bearer`, never in the
query string. A URL credential leaks where a header does not: any script on the page can
read the full URL out of `performance.getEntriesByType('resource')`, a DevTools HAR export
carries it into a bug report, and a reverse proxy logs it. There is deliberately **no**
client function that builds a token-bearing HTTP URL, so one cannot be reintroduced by
accident — `hostFetch(host, path, opts)` is the only way to call the API.

Two places still carry `?token=`, both unavoidable and both deliberate:

* **WebSocket dials** (`/ws`, `/control`, `/browserland`). The browser WebSocket API
  cannot set request headers on the handshake, so `hostWsUrl` appends the token. Closing
  this needs a short-lived connect-ticket scheme, not a refactor.
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
| `GET /`, `GET /help-corpus.json` | **the only unauthenticated responses.** The token is typed *into* that page and auth is query/header-only with no cookies, so gating the document deadlocks the bootstrap — every reload, bookmark and new tab would 401 forever. Neither carries host- or session-derived data. Headless, `GET /` is `200 {"ui": false}`, so health probes keep working. |
| `OPTIONS` preflights | unauthenticated by design (they carry no credentials). Explicit routes, because route resolution happens before request middleware. |
| `WS /browserland` (producers) | token required, loopback included. Was the one gate the token never covered — and WebSockets are not CORS-gated, so any website could dial `ws://127.0.0.1:4445/browserland`, re-register a live `window_id` (kicking the real agent off with 1012) and inject fabricated terminal output. Refusal is a post-upgrade WS close **4401** (an HTTP reject would surface as an opaque 1006). |
| `POST /launch` | token required — never an open RCE on any bind. |
| `WS /ws`, `WS /control`, `GET /sessions`, `GET /profiles` | token required (`?token=`, `?auth=`, or `Authorization: Bearer`). Always **401 `auth_required`**, never 403: the login overlay and the taskbar host chips pop on 401 only. |
| `GET`/`POST /profiles/config`, `GET /profiles/detect` (profile editor, #70) | browser token, same as `/file/*` and `/mcp/config`. Full commands are browser-realm only; `/profiles` and `/mcp/profiles` stay names-only. |
| `GET /status/fetch` (AI-provider status proxy, #112) | browser token, same as `/info` and `/state`. The broker's **only outbound HTTP**: an allowlist of AI-provider Atlassian Statuspage hosts, reached by provider **id** (the client passes ids, never a URL — unknown ids are dropped, an all-unknown request is `400`). Each fetch is **https-only, no-redirect, no-proxy, `200`-only, 512 KiB-capped, 4 s timeout**, with a 60 s per-id cache; any failure degrades to an `unknown` row (never blocks the UI). **Privacy**: enabling the (default-off) `aistatus` mod is what turns this on — the broker's egress IP then becomes visible to those status hosts, so it ships disabled until you opt in. |
| CORS (JSON API) | `Access-Control-Allow-Origin: *` **unconditionally on every response**, including the 401 — lets the multi-host UI on another broker's origin read this one, and without it a cross-origin login probe surfaces as a fetch TypeError ("wrong password" indistinguishable from "host down"). Auth is token-in-query/header and never cookies, so `*` grants no ambient credential; the real gate is the token on every route. |
| Response headers | `Referrer-Policy: no-referrer` (the desktop URL carries the token, so an outbound link must not leak it in `Referer`), `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'` (`GET /` is public so login can bootstrap, but an attacker page must not be able to iframe the real UI and clickjack a browser that already holds a token), and CSP `script-src` — see below. |

**Third-party code (#143).** The page used to pull xterm from jsdelivr and, lazily, CodeMirror from esm.sh — third-party code executing in the same origin that holds `prefs._hosts[].token` for **every** configured host, tokens that gate `/launch` and host-wide `/file/*`. Now:

* **xterm is vendored** into `webterm/broker/vendor/` and served by the broker at `/vendor/*` (public, like `GET /` — the browser needs it to render the login page, before any token exists). There is no CDN to compromise, no SRI hash to keep in sync, and the terminal now works **offline**, which it never did before. The bytes are the exact published npm files, pinned by sha384 in `tests/test_vendor_assets.py`; an upgrade is a commit that changes both. Served from an allowlist dict, so a client-supplied name can never reach the filesystem.
* **`script-src`** allows only `'self'`, `https://esm.sh`, and a `'sha256-…'` for the page's single inline `<script>`. The hash is computed at startup from the assembled `INDEX_HTML` itself (`ui.inline_script_hash`), so it tracks the bundle automatically and needs no `'unsafe-inline'` — meaning an **injected** inline script cannot execute either. Nothing else is set (no `default-src`/`style-src`/`connect-src`): those would have to enumerate every inline style, the `data:` favicon, `blob:` download URLs and every federated host, each a way to break the app for no gain here.

The one remaining third party is **CodeMirror from esm.sh**, loaded by dynamic `import()` when the text editor first opens. It cannot be integrity-pinned: its URLs are semver-range-resolved on purpose, because CodeMirror 6 needs one shared `@codemirror/state` instance and an exact pin that drifts silently kills syntax highlighting (see the comment in `mods/editor/codemirror.js`). Vendoring its ~50-module graph is the only real fix and is tracked separately.

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
(e.g. a WSL box over Tailscale). The **browser connects directly** to each
host (cross-origin `fetch /sessions` + `ws://host/ws`); hosts and their
passwords live per-browser in localStorage. Requirements:

* you need the remote broker's token (every broker has one — either
  configured or minted; `python -m webterm.broker --print-token` prints it)
  and a way in: either a non-loopback bind (plain http), or — recommended —
  a loopback bind fronted by `tailscale serve` / an https reverse proxy.
  Setting `auth_token` explicitly is worth it across a fleet so every host
  shares one password instead of a per-machine minted value;
* **both brokers must run this webterm version** — a pre-CORS remote shows
  up as a red "down" chip even when it's running;
* one scheme for the whole fleet — an https page fetching an http remote
  is blocked as mixed content, so cockpit + hosts must be all-https
  (preferred: unlocks secure-context features like clipboard image paste)
  or all-plain-http (see SETUP.md → Multiple machines over Tailscale).

One status chip per broker is always shown in the taskbar — even for a
single healthy local broker, so the per-broker hide toggle is always
reachable. Four states: green ok / red down / amber password-needed /
blue lease; ok/down chips toggle that host's window visibility, while
auth and lease chips are click-to-log-in / take-over. Window prefs are
keyed per host; a down host never closes or re-dials another host's
windows.

Remote agents against this broker:

```bash
BROWSERLAND_BROKER_URL='ws://broker-host:4445/browserland' \
WEB_TERMINAL_TOKEN='...' ./launchers/run-agent.sh -- bash -l
```

Any producer can register the same way:
`BROWSERLAND_BROKER_URL=ws://host:4445/browserland?token=...`.

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
re-assert postamble (`?2004h/l` + `?1h/l` per the agent's live sniffer,
#138) so a reloaded xterm.js recovers bracketed-paste and
application-cursor state. The browser→broker `paste` frame type is a
legacy alias for `input` (#138), kept only for tabs served before the UI
switched to xterm's `paste()`. Windows caveat: ConPTY never forwards an
app's `?2004h` request to the terminal side (verified live on Server
2022), so for a detected foreground Claude Code — which parses
`ESC[200~` regardless — the UI hand-brackets pastes when xterm's own
bracketed-paste mode is off (`pasteTextToTerm` in the window lifecycle
fragment).

Known limits: no scrollback replay, no alt-screen modeling in snapshots,
mouse not forwarded.

## MCP HTTP interface

The broker exposes an **opt-in HTTP API** (the `/mcp/*` surface) that an MCP
server wraps as MCP tools so an AI agent can list, observe, drive, and launch
terminals. A ready-to-run server ships in [`webterm/mcptool/`](../webterm/mcptool/)
(stdio transport; `pip install -e ".[mcp]"`). Nine token-gated endpoints make
up the contract:

| Method | Path | Purpose |
|---|---|---|
| GET | `/mcp/info` | feature flags (`allow_launch`, `default_mode`) |
| GET | `/mcp/terminals` | list MCP-visible terminals |
| POST | `/mcp/read` | render a terminal's screen as plain text |
| POST | `/mcp/input` | type into a terminal (`readwrite` only) |
| POST | `/mcp/reset` | clear a terminal's screen-render buffer (`readwrite` only) |
| POST | `/mcp/flush` | discard a terminal's unread queued input (`readwrite` only) |
| POST | `/mcp/pace` | set a terminal's default send_keys pacing (`readwrite` only) |
| GET | `/mcp/profiles` | launchable profile names |
| POST | `/mcp/launch` | spawn a new terminal from a profile |

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
| `off` | hidden from `/mcp/terminals`; `/mcp/read` + `/mcp/input` → **404 `unknown_or_off`** |
| `read` | visible + `/mcp/read`; `/mcp/input` → **403 `read_only`** |
| `readwrite` | read **and** `/mcp/input` |

* **`default_mode`** (global, default `off`) — applies to every window without an
  override; change it live in Control Panel → MCP access or `POST /mcp/config`.
* **per-window override** — set from the window title-bar **MCP access** menu or
  `POST /session/mcp`; **in-memory only** (resets on broker restart / agent
  relaunch).
* **`allow_launch`** (global, default `false`) — independent flag gating
  `/mcp/launch` only.

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

**`GET /mcp/info`** →
`{"ok":true,"allow_launch":false,"default_mode":"off","version":"0.1.0+ba4b62e"}`.
`version` is this broker's build id (`webterm.build_version()` — package version +
git short hash, or the bare package version off a checkout) for stale-deploy
detection (#22).

**`GET /mcp/terminals`** → an array; windows whose effective mode is `off` are
omitted. `agent` is the detected foreground-agent name (`""` when none), `kind`
the producer kind (`"agent"` vs a non-agent `"terminal"`), `mode` the effective
access mode. `version` is the producer's reported build id (`""` for a pre-#22
agent / a non-agent producer); for **agent** producers a `stale` boolean flags a
build differing from this broker's (a deploy predating a fix — reliable when
builds carry a git hash). `app_cursor` is the cached DECCKM (application-cursor
mode) the MCP `send_keys` reads to pick CSI vs SS3 arrows (#23); `pace_ms` is the
window's default `send_keys` inter-key pacing (#133, `0` = single-burst, set via
`/mcp/pace`) the MCP server reads so a no-`delay_ms` send auto-paces. `pyte` (#134)
is whether the agent has pyte installed: `false` means its `read_screen` uses the
dependency-free textgrid fallback — no `attr_runs` (#128) and no keyframe repair
(#130), so a sparse alt-screen frame after ring eviction comes back flagged
`partial` only. It defaults `true` for a pre-#134 agent that omits the field:

```json
[{"id":4503603655475937,"title":"bash","host":"JC-SERVER","cwd":"/home/me",
  "agent":"","kind":"agent","cols":80,"rows":24,"mode":"read",
  "version":"0.1.0+ba4b62e","stale":false,"app_cursor":false,"pace_ms":0,
  "pyte":true}]
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
client-side pacer. The value is **ephemeral per-connection** (resets on agent
relaunch, like a per-window mode override).

**`GET /mcp/profiles`** → `{"default":"cmd","profiles":["cmd","powershell"]}`
(the broker's configured `agent.profiles`; `bash`/`sh` on Linux).

**`POST /mcp/launch`** — requires `allow_launch` (else **403 `launch_disabled`**).
Body reuses the `/launch` shape: `{"profile": <str>, "cols": 80, "rows": 24,
"title": <str>, "cwd": <str>}`, all optional (dims default 80×24, `profile`
defaults to the broker default; `cwd` must be an existing dir). Response is the
launcher's:

```json
{"ok":true,"id":4503603655475937,"registered":true,"agent_pid":12345}
```

**200** when the agent registered within 10 s, **202** when it spawned but had
not said `hello` yet (`"registered":false`).

### Error reference

| Status | `error` | When |
|---|---|---|
| 403 | `mcp_disabled` | feature disabled or no token configured |
| 401 | `auth_required` | missing/invalid MCP token |
| 400 | `bad_json` | body is not a JSON object |
| 400 | `bad_id` | `id` missing or not an integer |
| 404 | `unknown_or_off` | no such window, or its effective mode is `off` |
| 400 | `bad_data` | `/mcp/input` `data` is not a string |
| 403 | `read_only` | `/mcp/input` on a window not in `readwrite` |
| 413 | `too_large` | `/mcp/input` payload > 256 KiB |
| 400 | `bad_pace` | `/mcp/pace` `pace_ms` missing or not an integer |
| 502 | `no_producer_rpc` | `/mcp/read` · `/mcp/reset` · `/mcp/flush` producer did not answer (non-agent / timeout) |
| 502 | `reset_failed` | `/mcp/reset` agent could not clear its render buffer |
| 502 | `flush_failed` | `/mcp/flush` agent could not flush its pending input |
| 403 | `launch_disabled` | `/mcp/launch` with `allow_launch:false` |
| 400 | `unknown_profile` | `/mcp/launch` profile not in config |
| 400 | `bad_dims` / `bad_cwd` / `cwd_not_dir` | `/mcp/launch` bad `cols`/`rows`/`cwd` |
| 429 | `too_many_pending_launches` | `/mcp/launch` backpressure |
| 500 | `spawn_failed` / `agent_exited_early` | `/mcp/launch` agent failed to start |

### Admin surface (browser `auth_token`-gated)

These configure MCP and are **not** part of the MCP token's surface. They use
the browser **`auth_token`** (the same mandatory-token gate as `/state` and
`/file/*`): always required, on every interface, as `Authorization: Bearer` or
`?token=`.

**`GET /mcp/config`** →
`{"ok":true,"enabled":false,"token":"","default_mode":"off","allow_launch":false,
"token_env_pinned":false}` (`token` is the live secret, `""` when unset).

**`POST /mcp/config`** — partial update of any of `enabled`, `default_mode`
(`off`/`read`/`readwrite`), `allow_launch`, `token`, or `generate:true` (mint a
fresh `token_urlsafe(32)`). Validated before any write; returns the GET shape.
While the env pins the token, `token`/`generate` edits are **ignored** (the live
token stays the env value). Errors: `bad_json` (400), `bad_mode` (400),
`bad_token` (400, non-string `token`).

**`POST /session/mcp`** — body `{"id": <int>, "mode": "off"|"read"|"readwrite"}`
→ `{"ok":true,"id":<int>,"mode":<str>}`. Sets the **in-memory** per-window
override (resets on restart / relaunch). `bad_mode` (400) on an invalid mode.

**Sidecar `webterm_mcp.json`** — the durable MCP config, written atomically next
to the `/state` store (default `<state_path dir>/webterm_mcp.json`; override with
config `mcp_state_path`). Schema:

```json
{"token": "<secret or null>", "default_mode": "off",
 "allow_launch": false, "enabled": true}
```

`token` is `null` when the env pins it (the secret stays off disk). The file
self-heals if hand-edited or truncated. **Per-window modes are not persisted** —
only these broker-wide knobs are.

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
catalog + the security rationale: **[PROFILES.md](PROFILES.md)**.

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
