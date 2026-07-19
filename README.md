<img width="2172" height="724" alt="image" src="https://github.com/user-attachments/assets/ec5413d6-24fd-4d4d-968f-7c13db156c8d" />


# Browserland

**A remote control plane for every machine you own: install Browserland on
each one, and a single browser tab becomes a desktop of live terminals across
all of them — including the AI coding agents running inside.**

Point a browser at one broker and you get a full windowed desktop of live
terminals: tile them, tab them, split them, and drag them across virtual
desktops. Each terminal is a real PTY running on some machine, streamed to the
browser over a WebSocket. Add your other machines as hosts (token-auth'd, e.g.
over [Tailscale](https://tailscale.com/)) and their terminals appear in the
same desktop — launch a shell on the laptop, the desktop, and the server from
one **+** menu, side by side.

The shells keep running even when no browser is attached. Close the tab, come
back tomorrow, and the screen heals from a snapshot — exactly where you left it.

The name says it plainly: a whole little desktop — windows, terminals, and your
fleet of AI coding agents — living entirely in a browser tab. (`webterm` is the
Python package and module name.)

Browserland also exposes an **MCP server**, letting LLM harnesses drive the
terminals directly — including full-screen TUIs.

## What is this?

Browserland is two small programs and a browser:

- **Agents** (producers) are headless processes that own a real terminal —
  `pty.openpty` on Linux, ConPTY/WinPTY on Windows. An agent runs a command
  (`bash`, `cmd.exe`, an AI coding agent, anything), keeps a ring buffer of
  recent output, and streams the terminal over a single WebSocket.
- **The broker** is a small web server. Agents register with it; browsers
  connect to it. It relays bytes both ways, serves the desktop UI, and can
  spawn new agents on demand from a list of pre-approved **profiles**.
- **The browser** renders the desktop — a tiling window manager over
  [xterm.js](https://xtermjs.org/) — and sends your keystrokes back to the PTY.

The wire format is a deliberately small set of JSON frames over one WebSocket,
so **any** producer that speaks them can register with the broker —
Browserland's own agent is just the reference implementation.

```
┌─────────┐  binary ANSI + JSON   ┌────────┐  /ws?session=<id>  ┌─────────┐
│  agent  │ ──────────────────▶  │ broker │ ──────────────────▶ │ browser │
│ (PTY +  │ ◀──────────────────  │        │ ◀────────────────── │ xterm.js│
│ ConPTY/ │  input/resize/       └────────┘  input/paste/resize └─────────┘
│ openpty)│  snapshot_please
└─────────┘
```

Because the PTY lives in the agent, not the browser, terminals survive browser
reloads and even broker restarts — the agent reconnects with backoff and the
browser's attach triggers a snapshot redraw from the ring buffer.

## One tab, every machine

The part people miss: Browserland is a **remote control plane**, not just a
terminal for the machine it runs on. Run a broker on each of your machines,
then register them as **hosts** in the UI (**Control Panel → Hosts** — a name,
a URL, and that broker's token; a [Tailscale](https://tailscale.com/) address
works great). One browser tab then fronts the whole fleet:

```
                       one browser tab
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
     ┌─────────┐        ┌─────────┐        ┌─────────┐
     │ broker  │        │ broker  │        │ broker  │   one per machine
     │ laptop  │        │ desktop │        │ server  │   (LAN / Tailscale)
     └─┬─────┬─┘        └─┬─────┬─┘        └─┬─────┬─┘
       ▼     ▼            ▼     ▼            ▼     ▼
     shell  claude      shell  build       shell  claude   your PTYs + agents
```

- The right-click **+** menu groups launch profiles **per host** — open a
  PowerShell on the Windows box and a zsh on the Linux server from the same
  menu, and tile them next to each other.
- Every window is badged with its host; per-host status chips in the taskbar
  show at a glance which brokers are reachable.
- Each host keeps its own token, profiles, and file API — the UI fans out,
  the brokers stay independent. A machine going down takes its windows
  stale, not the desktop.
- The MCP server speaks the same multi-host language: one `--hosts` list lets
  an AI harness enumerate and drive terminals across **all** brokers through
  a single tool surface.

**[`docs/SETUP.md`](docs/SETUP.md)** walks through joining machines step by
step.

## Screenshots 

The desktop with tiled and floating terminals

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/135f7d46-617e-40f4-a195-f1ce43a625eb" />

<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/db268062-9932-498c-9e55-1fab4ce50422" />


## Features

- **In-browser tiling window manager** (niri-style): floating *and* tiled
  windows, a virtual-desktop pager, and a taskbar — all in the page, no native
  app.
- **Nested tabs and splits**: any tile can hold a tab group, and tabs can hold
  splits, via a recursive cell model. Build the layout you want by dragging.
- **More than terminals**: sticky notes, a [CodeMirror 6](https://codemirror.net/)
  text editor, a file manager, a task manager, a synced scratchpad, a clipboard
  history, and a session recorder — each a bundled **mod** (see
  [Mods](#mods)) over the broker's token-gated `/file/*` and per-mod store APIs.
- **Session recording & replay**: hit **⏺** on any terminal to record it
  byte-faithfully, then play it back at the original size — pause, 0.25×–8×
  speed, **continuous rewind**, and notes pinned to timestamps.
- **A bundled mod system**: the desktop's app windows and widgets are mods
  over a small, versioned `ctx` API — a mod is one folder with a manifest and
  an entry script, and it can add window kinds, per-terminal title-bar
  widgets, taskbar chips, Control-Panel settings, and in-app help pages.
- **Cross-platform PTY**: Linux `pty.openpty`; Windows auto-selects ConPTY or
  WinPTY — even headless/detached agents acquire a hidden console and re-enable
  Ctrl-C so they run ConPTY with a working interrupt and live resize, falling
  back to WinPTY only if console acquisition fails (or when forced via
  `--pty-backend winpty`).
- **AI agent fleet**: detects the foreground coding agent in each window
  (`claude` / `codex` / `grok` / `opencode`) and tracks live OSC title +
  working directory, with an opt-in per-window git-status widget (the `git`
  mod).
- **Multi-host — the control plane**: attach the same UI to the brokers on all
  your other machines (e.g. over [Tailscale](https://tailscale.com/)) and run
  their terminals side by side in one tab, with per-host status chips in the
  taskbar. See [One tab, every machine](#one-tab-every-machine).
- **Single active-browser lease**: exactly one browser drives input at a time,
  so two open tabs never fight over the keyboard.
- **Opt-in MCP / AI agent access**: an MCP client or AI harness can list,
  observe, drive, and launch terminals — including live interactive TUIs and the
  console you're working in — under per-window access modes you control.
  See **[MCP & AI agent access](#mcp--ai-agent-access)** below.
- **Token auth, no open RCE**: one token gates non-loopback access and doubles
  as the UI password; launching is **profiles-only** (the client can never
  supply a raw command), with a loopback exemption for local use.
- **Launch profiles you can edit in the UI**: add WSL/zsh/PowerShell shells from
  **Control Panel → Launch profiles** (one-click WSL-distro / shell **Detect…**),
  applied live with no restart — still profiles-only. See
  **[docs/PROFILES.md](docs/PROFILES.md)** for recipes and the security model.

## Mods

The desktop's app windows and most of its optional chrome ship as **mods** —
the terminal pipeline, window manager, multi-host, and MCP surfaces stay
core. A mod is a self-contained folder under `webterm/broker/mods/<id>/`
holding a manifest (`mod.json`), one entry script, and optional CSS + an
in-app help page. Mods are trusted first-party code: the broker splices an
explicit allow-list of them into the single served page (there is no runtime
plugin install). Each registers through a versioned `ctx` API that exposes
per-terminal-window hooks, app-window kinds (which appear in the **+** launch
menu), taskbar chips, Control-Panel settings, a durable per-mod server store
with revision history, an in-desktop copy/paste observer, and help cards.

A mod's *settings* sync across your browsers via the broker's shared state;
its *enable/disable* toggle is deliberately per-browser — flip it in
**Control Panel → Mods**. A broker-side `mods_enabled` master switch gates
the whole system. The desktop ships with fifteen:

| Mod | What it adds | Default |
|---|---|---|
| `theme` | color-scheme picker for the desktop chrome | on |
| `pattern` | desktop background patterns | on |
| `clock` | taskbar clock with timezone picker | on |
| `help` | the in-app help window + **?** chip | on |
| `sticky` | sticky notes | on |
| `editor` | CodeMirror 6 text editor | on |
| `file-manager` | dual-pane file manager | on |
| `task-manager` | live per-host process list | on |
| `scratchpad` | server-backed notes, synced across browsers with revision history | on |
| `agent-docs` | AGENTS.md / CLAUDE.md one-click openers on terminal title bars | on |
| `recorder` | terminal session recorder + fixed-size player (speed, continuous rewind, timestamped notes) | on |
| `git` | per-terminal git branch + dirty-state widget | off |
| `aistatus` | AI-provider status chip + window | off |
| `clipboard` | rolling history of copies/pastes made through the desktop | off |
| `termfont` | terminal font picker | off |

`clipboard` and `aistatus` are off by default on principle — clipboards carry
secrets, and status polling talks to third-party endpoints; `git` and
`termfont` are simply opt-in preferences. Enabling any of them is one click
in the Mods pane.

## MCP & AI agent access

The broker exposes a token-gated `/mcp/*` HTTP API and ships a stdio
[MCP](https://modelcontextprotocol.io/) server (`webterm.mcptool`), so any MCP
client or AI harness can **list, observe, drive, and launch** terminals. The
agents are just producers; the broker stays the sole authority — every MCP call
is gated by the same per-window access modes and the master enable switch.

- **Interactive TUIs, as plain text** — `read_screen` renders the *current*
  screen of a live terminal by replaying its PTY ring buffer through
  [pyte](https://github.com/selectel/pyte), so a harness can read full-screen
  apps (btop, htop, vim, less) — not just line-oriented scrollback — and
  `send_input` types into them. Without pyte the read falls back to a
  dependency-free in-house renderer that still returns a **bounded** rendered
  grid (`degraded: true` is now reserved for a rare last-ditch raw decode).
- **The live session you're working in** — `list_terminals` enumerates running
  sessions (id, title, cwd, agent, kind, cols/rows, mode), so a harness can
  attach to the exact console a person is using right now, read its state, and
  (in `readwrite`) drive it. Sessions persist across browser reloads and broker
  restarts, so the handle stays valid.

### Tools

Each tool maps to a broker endpoint and returns its JSON. Window `id`s are
namespaced `"<host>:<int>"` strings so one server can front several brokers (see
**Multi-host** below); with a single broker the host is `default`
(`"default:12345"`).

| Tool | Endpoint | Notes |
|---|---|---|
| `mcp_info(host?)` | `GET /mcp/info` | feature flags (`allow_launch`, `default_mode`). Omit `host` → dict keyed by host name |
| `list_terminals` | `GET /mcp/terminals` | `{"terminals":[…], "errors":{host:msg}}`: all hosts merged, each terminal's `host` set to the config name (broker's machine hostname preserved as `machine_host`) + namespaced `id`; a down host lands in `errors` without sinking the rest. Each terminal carries a `pyte` flag — `false` = the agent lacks pyte, so `attr_runs`/keyframe-repair are unavailable and sparse alt-screen frames are flagged `partial` only (#134) |
| `list_profiles(host?)` | `GET /mcp/profiles` | launchable profile names + default. Omit `host` → dict keyed by host name |
| `read_screen(id)` | `POST /mcp/read` | screen rendered as a bounded plain-text grid (pyte, or a dependency-free fallback); result carries `content_hash`, `stable_hash` (the cursor-blind digest — a cursor blink in place doesn't change it, a cursor move does), and `idle_ms` (best-effort ms since the last PTY output — absent from older agents, unreliable for a perpetually-animating app). Waits (one call, `timeout_ms`-bounded, mutually exclusive): `wait_for_change` / `wait_for_text` / `wait_for_regex` / `wait_for_idle` (block until the screen settles — `stable_hash` unchanged for N ms) |
| `send_input(id, data)` | `POST /mcp/input` | target window must be in **`readwrite`** mode; newlines are sent as **Enter** (CR) so commands run (incl. on PowerShell) |
| `send_keys(id, keys, delay_ms?)` | `POST /mcp/input` | send control/escape **keys** — `["C-c"]`, `["Esc"]`, `["Up","Enter"]` — that plain text can't express; `delay_ms` (or a per-terminal `set_pace` default) writes one key per POST for a frame-polling TUI |
| `set_pace(id, pace_ms)` | `POST /mcp/pace` | **`readwrite`**; set a per-terminal DEFAULT send_keys pacing (ms, capped 1000, `0` disables) so multi-key sends auto-pace without passing `delay_ms` — for a frame-polling TUI (Dwarf Fortress). Broker-local + ephemeral (resets on agent reconnect) |
| `reset_terminal(id)` | `POST /mcp/reset` | **`readwrite`**; correlated round-trip that wipes the agent's screen-render buffer so the next `read_screen` starts clean (**502** on a non-agent producer) |
| `flush_input(id)` | `POST /mcp/flush` | **`readwrite`**; correlated round-trip that discards keystrokes queued to the app but not yet consumed — the input-side mirror of reset (**502** on a non-agent producer; a no-op on a Windows/ConPTY agent) |
| `launch_terminal(profile?, cols=80, rows=24, title?, cwd?, host?)` | `POST /mcp/launch` | broker must have **`allow_launch`** enabled; `host` required when multiple hosts are configured |

**Multi-host.** Pass `--hosts` (or `$BROWSERLAND_MCP_HOSTS`) a JSON array of
`{name, url, token}` descriptors to serve N brokers from one server process;
every id-taking tool routes on the `"<host>:…"` prefix. The single
`--broker-url`/`--token` form is the one-host shorthand (`default`). See
[`webterm/mcptool/README.md`](webterm/mcptool/README.md) for details.

> The `send_input` **tool** maps newlines in `data` to a carriage return — the
> byte a real Enter key sends — so a line submits on PowerShell/PSReadLine (which
> treats a bare line-feed as a soft continuation) and on a Unix shell alike. The
> raw `POST /mcp/input` endpoint stays **verbatim**: drive it directly to send a
> literal LF or hand-crafted control/escape bytes.

### Safety / enabling

Access is layered and opt-in — nothing is reachable until you turn it on:

- **Master enable** is **off by default**; while off, every `/mcp/*` call returns
  `403 mcp_disabled`.
- **Per-window mode** is `off` / `read` / `readwrite`, with a global
  `default_mode` for new windows. `off` hides a window entirely; `read` allows
  observation; `readwrite` additionally allows `send_input`.
- **`allow_launch`** is a separate gate for `launch_terminal`.
- The **MCP token** is a bearer secret distinct from the browser `auth_token`
  (the UI password): the **broker** pins it with `WEB_TERMINAL_MCP_TOKEN` (or the
  `webterm_mcp.json` sidecar), and the **MCP client** passes the same secret via
  `BROWSERLAND_MCP_TOKEN` (as the harness examples below show).

### Register with a harness

```bash
claude mcp add browserland \
  --env BROWSERLAND_MCP_TOKEN=… \
  --env BROWSERLAND_MCP_URL=http://127.0.0.1:4445 \
  -- python -m webterm.mcptool
```

Any other **stdio** MCP client (Hermes, your own, …) registers the same way —
point it at the launch command and pass the two env vars:

```json
{
  "mcpServers": {
    "browserland": {
      "command": "python",
      "args": ["-m", "webterm.mcptool"],
      "env": {
        "BROWSERLAND_MCP_TOKEN": "…",
        "BROWSERLAND_MCP_URL": "http://127.0.0.1:4445"
      }
    }
  }
}
```

Or run the server directly, talking to the local broker:

```bash
BROWSERLAND_MCP_TOKEN=… python -m webterm.mcptool
BROWSERLAND_MCP_TOKEN=… browserland-mcp --broker-url http://127.0.0.1:4445
```

For the full HTTP contract, error table, and config sidecar, see
**[`docs/TECHNICAL.md`](docs/TECHNICAL.md)** and
**[`webterm/mcptool/README.md`](webterm/mcptool/README.md)**.

## Quick start

You need **Python ≥ 3.9**. Install from a checkout:

```bash
pip install -e .
```

### Windows

```powershell
# broker (default 127.0.0.1:4445)
python -m webterm.broker

# an agent running cmd.exe, registered with the local broker
python -m webterm.agent -- cmd.exe

# then open http://127.0.0.1:4445/ and click the session (or "new terminal")
```

Windows agents also need a PTY backend: `pip install -e ".[windows]"` (pulls in
`pywinpty`).

### Linux

```bash
./launchers/run-broker.sh                  # broker
./launchers/run-agent.sh -- bash -l        # agent
```

The launcher scripts bootstrap a virtualenv with the runtime dependencies on
first run. Then open `http://127.0.0.1:4445/`.

## Install & extras

```bash
pip install -e .                       # core: broker + agent
pip install -e ".[windows]"            # + pywinpty (Windows PTY backend)
pip install -e ".[pyte]"               # + pyte (tier-2 snapshot rendering)
pip install -e ".[procs]"              # + psutil (task manager, agent badge, live cwd)
pip install -e ".[mcp]"                # + the stdio MCP server (Python ≥ 3.10)
pip install -e ".[dev]"                # + pytest for the test suite
```

`psutil` (the `procs` extra) is best-effort: it powers the **task-manager
process list**, the **foreground-agent badge**, and **live-cwd tracking**.
Without it the agent still runs and still destroys windows — those three views
just degrade (empty list / no badge / no cwd).

Mix and match, e.g. `pip install -e ".[pyte,mcp,dev]"`. The `mcp` extra requires
**Python ≥ 3.10** (the MCP SDK), while everything else runs on **Python ≥ 3.9**.
See **[MCP & AI agent access](#mcp--ai-agent-access)** for running the server.

## Project layout

| Path | What |
|---|---|
| `webterm/agent/` | Headless producer: PTY backends, output ring buffer, OSC-title sniffer, reconnecting WebSocket client |
| `webterm/broker/` | Web server: desktop UI (`ui.py` assembles the served page from ordered `*.html`/`*.css`/`*.js` fragments), `/ws` relay, producer WS, session list, profiles-only launch |
| `webterm/broker/mods/` | The bundled desktop mods — one folder per mod: `mod.json` manifest, entry script, optional CSS + in-app help page |
| `webterm/mcptool/` | The shipped stdio MCP server wrapping the broker's `/mcp/*` API |
| `webterm/protocol.py` | The single source of truth for the JSON frame shapes |
| `launchers/` | venv-bootstrapping run scripts (and systemd units) for both OSes |
| `tests/` | pytest suite: protocol, snapshots, agent↔broker integration, real-PTY round trips |

## Documentation

New here, or setting up more than one machine? Start with
**[`docs/SETUP.md`](docs/SETUP.md)** — the onboarding guide: the broker / agent /
browser mental model, joining machines over Tailscale via Control Panel → Hosts,
what *not* to hand-edit, and running the broker unattended in the background
(Windows Task Scheduler / Linux systemd). It's written to be followed by a human
or a coding agent.

Adding or editing launch profiles (WSL / zsh / PowerShell / Git-Bash) is covered
in **[`docs/PROFILES.md`](docs/PROFILES.md)** — the recipe catalog, the three
profile fields, the `webterm_profiles.json` sidecar-vs-`broker_config` rule, and
the browser-realm-only editing model.

The full engineering reference lives in **[`docs/TECHNICAL.md`](docs/TECHNICAL.md)**:

- the complete wire protocol and frame semantics,
- the full auth model (every surface, token precedence, CORS),
- every HTTP endpoint including the MCP HTTP contract and its error table,
- access modes, the MCP + profiles config sidecars, and the shipped MCP server, and
- deployment notes (systemd units, multi-host over Tailscale, testing).

## License

Released under the MIT License — see [LICENSE](LICENSE).


not the.primeagen@gmail.com but hi :) 