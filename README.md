<img width="2172" height="724" alt="image" src="https://github.com/user-attachments/assets/ec5413d6-24fd-4d4d-968f-7c13db156c8d" />


# Browserland

**A web-based terminal desktop that launches and recovers a fleet of headless
shells — and the AI coding agents running inside them.**

Point a browser at the broker and you get a full windowed desktop of live
terminals: tile them, tab them, split them, drag them across virtual desktops.
Each terminal is a real PTY running on some machine, streamed over a WebSocket.
The shells keep running even when no browser is attached — close the tab, come
back tomorrow, and the screen heals from a snapshot.

The name says it plainly: a whole little desktop — windows, terminals, and your
fleet of AI coding agents — that lives entirely in a browser tab. (`webterm` is
the Python package/module name.)

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
  text editor, a file manager, and a task manager — backed by a sandboxed
  `/file/*` API.
- **Cross-platform PTY**: Linux `pty.openpty`; Windows auto-selects ConPTY or
  WinPTY (ConPTY when a console window exists for correct Ctrl-C handling,
  WinPTY for headless processes).
- **AI agent fleet**: detects the foreground coding agent in each window
  (`claude` / `codex` / `grok` / `opencode`), tracks live OSC title + working
  directory, and surfaces per-window git status.
- **Multi-host**: attach the same UI to additional brokers (e.g. another machine
  over [Tailscale](https://tailscale.com/)), with per-host status chips in the
  taskbar.
- **Single active-browser lease**: exactly one browser drives input at a time,
  so two open tabs never fight over the keyboard.
- **Opt-in MCP interface**: a token-gated HTTP API plus a shipped stdio
  [MCP](https://modelcontextprotocol.io/) server, so an AI agent can list, read,
  drive, and launch terminals — under per-window access modes you control.
- **Token auth, no open RCE**: one token gates non-loopback access and doubles
  as the UI password; launching is **profiles-only** (the client can never
  supply a raw command), with a loopback exemption for local use.

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
pip install -e ".[mcp]"                # + the stdio MCP server (Python ≥ 3.10)
pip install -e ".[dev]"                # + pytest for the test suite
```

Mix and match, e.g. `pip install -e ".[pyte,mcp,dev]"`. The `mcp` extra requires
**Python ≥ 3.10** (the MCP SDK), while everything else runs on **Python ≥ 3.9**.

The shipped MCP server is launched as a module (`python -m webterm.mcptool`) or
via the installed `carrier-mcp` console script.

## Project layout

| Path | What |
|---|---|
| `webterm/agent/` | Headless producer: PTY backends, output ring buffer, OSC-title sniffer, reconnecting WebSocket client |
| `webterm/broker/` | Web server: desktop UI (`index.html`), `/ws` relay, producer WS, session list, profiles-only launch |
| `webterm/mcptool/` | The shipped stdio MCP server wrapping the broker's `/mcp/*` API |
| `webterm/protocol.py` | The single source of truth for the JSON frame shapes |
| `launchers/` | venv-bootstrapping run scripts (and systemd units) for both OSes |
| `tests/` | pytest suite: protocol, snapshots, agent↔broker integration, real-PTY round trips |

## Documentation

The full engineering reference lives in **[`docs/TECHNICAL.md`](docs/TECHNICAL.md)**:

- the complete wire protocol and frame semantics,
- the full auth model (every surface, token precedence, CORS),
- every HTTP endpoint including the MCP HTTP contract and its error table,
- access modes, the MCP config sidecar, and the shipped MCP server, and
- deployment notes (systemd units, multi-host over Tailscale, testing).

## License

Released under the MIT License — see [LICENSE](LICENSE).
