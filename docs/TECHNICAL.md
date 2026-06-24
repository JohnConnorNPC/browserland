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
| `webterm/agent/backends/win_conpty.py` | Windows PTY via low-level `winpty.PTY` + reader thread (Proactor loop has no `add_reader` for pipes). **Backend auto-selection:** ConPTY silently drops the `0x03` → `CTRL_C_EVENT` translation when the hosting process has no console window (verified empirically — exactly the headless cases this agent exists for), so `--pty-backend auto` picks ConPTY only when a console window exists and WinPTY otherwise; `conpty`/`winpty` force it. |
| `webterm/agent/snapshot/raw.py` | tier-1 snapshot: `ESC[0m ESC[2J ESC[H` + ring replay |
| `webterm/agent/snapshot/pyte_snap.py` | tier-2 (optional, `--snapshot-mode pyte`): replay ring through pyte, render the settled grid |
| `webterm/broker/` | Sanic app: picker page, `/ws` relay, `/browserland` producer WS, `/sessions`, profiles-only `POST /launch` |
| `launchers/` | venv-bootstrapping run scripts for both OSes |

## Agent

```
python -m webterm.agent [opts] [--] command...
  --broker-url URL     $BROWSERLAND_BROKER_URL > flag > ws://127.0.0.1:4445/browserland
  --auth-token TOK     $WEB_TERMINAL_TOKEN; appended as ?token= (only needed
                       for non-loopback brokers)
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

## Broker

```
python -m webterm.broker [--host 127.0.0.1] [--port 4445] [--config PATH]
```

Config (`broker_config.json`, path overridable via `$WEB_TERMINAL_CONFIG`;
see `broker_config.example.json`): `auth_token`, plus `agent.profiles` for
`/launch`. **Profiles only** — `/launch` accepts
`{"profile": "cmd", "cols": 120, "rows": 32, "title": "..."}` and never a
client-supplied command/cwd/env. Responses: `200` registered, `202` spawned
but no hello within 10 s, `400` unknown profile, `401`/`403` auth, `429`
too many pending, `500` agent exited early.

### Auth model

Token from `$WEB_TERMINAL_TOKEN` (env wins) or config `auth_token`;
compared with `hmac.compare_digest`.

| Surface | Rule |
|---|---|
| `WS /browserland` (producers) | loopback exempt; non-loopback needs `?token=` (or `?auth=`). No token configured → non-loopback refused. Refusal is a post-upgrade WS close **4401** (an HTTP reject would surface as an opaque 1006). |
| `POST /launch` | token required whenever configured; without a token only loopback is allowed (403 `launch_disabled_no_token`) — never an open RCE on a non-loopback bind. |
| `WS /ws`, `GET /sessions` | gated by the token only when one is configured (`?token=`, `?auth=`, or `Authorization: Bearer`). |
| CORS (JSON API) | emitted **only when a token is configured** (`Access-Control-Allow-Origin: *` on every response incl. 401/404, explicit OPTIONS preflights on `/sessions` `/profiles` `/launch`) — lets the multi-host UI on another broker's origin read this one. A tokenless loopback-only broker emits no CORS headers and stays unreadable to arbitrary websites. |

Tokens are passed to spawned agents via **env only** (never argv — visible
in process lists), and auth failures log only path + client IP (the token
rides in query strings).

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

Settings → Hosts lets the UI attach to sessions on additional brokers
(e.g. a WSL box over Tailscale). The **browser connects directly** to each
host (cross-origin `fetch /sessions` + `ws://host/ws`); hosts and their
passwords live per-browser in localStorage. Requirements:

* the remote broker must have an `auth_token` configured (CORS is
  token-gated, so a tokenless remote is unreachable by design — the add
  form requires a password) and a non-loopback bind;
* **both brokers must run this webterm version** — a pre-CORS remote shows
  up as a red "down" chip even when it's running;
* serve the page over plain http when remotes are http (an https page
  fetching an http remote is blocked as mixed content).

Per-host status chips appear in the taskbar (green ok / red down / amber
password-needed, click to log in) only when >1 host is configured or some
host is unhealthy — the single-host UI is unchanged. Window prefs are
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
escape hatch when login-shell rc noise is unwanted). Set `auth_token`
(or `$WEB_TERMINAL_TOKEN`) **before** binding to anything non-loopback —
without a token the broker refuses non-loopback producers and `/launch`.

**systemd**: installable units live in `launchers/systemd/`
(`webterm-broker.service`, `webterm-agent.service`). Copy to
`/etc/systemd/system/`, edit `User=`, the `/opt/web_terminal` paths and
the token, then `systemctl daemon-reload && systemctl enable --now ...`.
Caveats: the `bash -l` profile sources the service user's login profile
(use the `sh` profile if that produces noise), and `/launch`-spawned
agents are session leaders that survive broker restarts — they
re-register within ~10 s of the broker coming back.

**Tests on Linux**: `python -m pytest tests -q` → expect
**126 passed, 2 skipped** (the skips are the Windows-ConPTY e2e). This
includes a real-POSIX-PTY suite (`tests/test_linux_pty.py`) that was
written blind on Windows — `LINUX_VERIFICATION.md` is the full
verification checklist to run before deploying.

## Protocol (reference)

Producer → broker: `hello` (required first frame; same `window_id`
replaces), `title`, `resized` as JSON text; raw ANSI output **and**
snapshots as unframed binary. Broker → producer: `input`, `resize` (reply
`resized`), `snapshot_please` (reply one binary full redraw). Browser
attach: broker sends `resized` *before* requesting the snapshot. Snapshots
start `ESC[0m ESC[2J ESC[H` — deliberately no `ESC c`/`ESC[3J`, since
already-attached browsers receive them too.

Known limits: no scrollback replay, no alt-screen modeling in snapshots,
mouse not forwarded.

## MCP HTTP interface

The broker exposes an **opt-in HTTP API** (the `/mcp/*` surface) that an MCP
server wraps as MCP tools so an AI agent can list, observe, drive, and launch
terminals. A ready-to-run server ships in [`webterm/mcptool/`](webterm/mcptool/)
(stdio transport; `pip install -e ".[mcp]"`). Six token-gated endpoints make up
the contract:

| Method | Path | Purpose |
|---|---|---|
| GET | `/mcp/info` | feature flags (`allow_launch`, `default_mode`) |
| GET | `/mcp/terminals` | list MCP-visible terminals |
| POST | `/mcp/read` | render a terminal's screen as plain text |
| POST | `/mcp/input` | type into a terminal (`readwrite` only) |
| GET | `/mcp/profiles` | launchable profile names |
| POST | `/mcp/launch` | spawn a new terminal from a profile |

The interface is **disabled by default** — nothing under `/mcp/*` answers until
an admin both enables it and sets a token (below). The broker serves the picker
page and this API, not this README, so editing these docs needs no restart.

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
**only** the `/mcp/*` data plane — there is **no loopback exemption** (MCP is
opt-in, so even a local caller needs the token). CORS preflights exist for
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
| `GET /mcp/profiles` | token | profile names |
| `POST /mcp/launch` | `allow_launch` | spawn a terminal |

**`GET /mcp/info`** → `{"ok":true,"allow_launch":false,"default_mode":"off"}`

**`GET /mcp/terminals`** → an array; windows whose effective mode is `off` are
omitted. `agent` is the detected foreground-agent name (`""` when none), `kind`
the producer kind (`"agent"` vs a non-agent `"terminal"`), `mode` the effective
access mode:

```json
[{"id":4503603655475937,"title":"bash","host":"JC-SERVER","cwd":"/home/me",
  "agent":"","kind":"agent","cols":80,"rows":24,"mode":"read"}]
```

**`POST /mcp/read`** — body `{"id": <int>}`:

```json
{"ok":true,"id":4503603655475937,"cols":80,"rows":24,"text":"<screen lines>\n..."}
```

The agent renders the screen through pyte off its event loop. If pyte is
unavailable on the agent the text is a best-effort raw decode and the response
adds **`"degraded": true`**. Only **agent** producers answer; a non-agent
terminal producer has no handler, so the request times out → **502
`{"error":"no_producer_rpc"}`**.

**`POST /mcp/input`** — body `{"id": <int>, "data": "<str>"}` → `{"ok":true}`.
Requires effective mode `readwrite` (else **403 `read_only`**); `data` must be a
string (else **400 `bad_data`**) and ≤ **256 KiB** UTF-8 (else **413
`too_large`**). Forwarded straight to the PTY and **deliberately bypasses the
single-active-browser lease** — MCP is its own authorized channel.

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
| 502 | `no_producer_rpc` | `/mcp/read` producer did not answer (non-agent / timeout) |
| 403 | `launch_disabled` | `/mcp/launch` with `allow_launch:false` |
| 400 | `unknown_profile` | `/mcp/launch` profile not in config |
| 400 | `bad_dims` / `bad_cwd` / `cwd_not_dir` | `/mcp/launch` bad `cols`/`rows`/`cwd` |
| 429 | `too_many_pending_launches` | `/mcp/launch` backpressure |
| 500 | `spawn_failed` / `agent_exited_early` | `/mcp/launch` agent failed to start |

### Admin surface (browser `auth_token`-gated)

These configure MCP and are **not** part of the MCP token's surface. They use
the browser **`auth_token`** (the same token-or-loopback gate as `/state` and
`/file/*`): when an `auth_token` is configured they require it (`Authorization:
Bearer` / `?token=`); on a tokenless broker only loopback is allowed.

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

### curl

```bash
B=http://127.0.0.1:4445

# Admin (loopback, or add -H "Authorization: Bearer $AUTH" on a token broker):
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

A ready-to-run MCP server lives in [`webterm/mcptool/`](webterm/mcptool/) — a
thin stdio wrapper that maps each endpoint above to an MCP tool (`mcp_info`,
`list_terminals`, `list_profiles`, `read_screen`, `send_input`,
`launch_terminal`). It connects to the broker over HTTP and authenticates with
the MCP token.

```bash
pip install -e ".[mcp]"                      # needs Python >=3.10 (the mcp SDK)
claude mcp add browserland --env BROWSERLAND_MCP_TOKEN=… -- python -m webterm.mcptool
```

See [`webterm/mcptool/README.md`](webterm/mcptool/) for config (env/flags), the
tool list, and how access modes / `allow_launch` still govern behavior.

## Tests

```powershell
python -m pytest tests -q
```

128 tests collected (124 run on Windows, 126 on Linux — the platform PTY
e2e suites skip on the other OS): protocol shapes, ring eviction, OSC
sniffer split at every byte index, raw/pyte snapshot rendering,
agent↔fake-broker integration (reconnect, snapshot ordering, title
re-hello), real-ConPTY round trip on Windows, real-POSIX-PTY round trip
on Linux (bash echo/resize/`stty size`/OSC title/exit code, Ctrl-C via
TIOCSCTTY, huge-paste backpressure), broker e2e as a subprocess (auth
gates, CORS with/without a token incl. error paths and preflights,
sanic-ext neutralized, relay invariants, `/launch` → detached agent on
both platforms), and non-loopback auth negatives + CORS positives via
the box's LAN IP.

`pyte` is optional: `pip install pyte` (only needed for
`--snapshot-mode pyte`; `raw` works without it). Windows agents need
`pywinpty>=2`.

## Relationship to xterm-py

Read-only reference: `X:\Data\xterm-py` (the relay/registry/UI source this
broker's relay and registry were adapted from). **Not modified by this
project.** This broker defaults to port **4445**.
