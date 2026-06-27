# Browserland MCP server (`webterm.mcptool`)

A thin **MCP server** (stdio transport) over a Browserland broker's token-gated
`/mcp/*` HTTP interface. It lets any stdio MCP client — Claude Code, Hermes, or
your own — **list, observe, drive, and launch** Browserland terminals as MCP tools.

It is a wrapper, not a new authority: the broker still governs everything. A
window must be in `read`/`readwrite` mode to be visible or typed into, and
launching requires the broker's `allow_launch` flag. See the root
[`README.md`](../../README.md) → **MCP HTTP interface** for the broker contract,
access modes, and how to enable MCP + mint the token.

## Install

The MCP SDK requires **Python ≥ 3.10** (webterm core stays ≥ 3.9), so it lives
in an optional extra:

```bash
pip install -e ".[mcp]"
```

This pulls in `mcp` (FastMCP) and `httpx`.

## Configure

The server connects to the broker over HTTP and authenticates with the **MCP
token** (the secret in `webterm_mcp.json` — *not* the browser `auth_token`).
Config resolves **flag > env > default**:

| What | Flag | Env | Default |
|---|---|---|---|
| Broker base URL | `--broker-url` | `BROWSERLAND_MCP_URL` | `http://127.0.0.1:4445` |
| MCP token | `--token` | `BROWSERLAND_MCP_TOKEN` (or `WEB_TERMINAL_MCP_TOKEN`) | — (required) |
| Token from sidecar | `--token-file PATH` | — | — |
| Multiple brokers | `--hosts JSON` | `BROWSERLAND_MCP_HOSTS` | — (single-host) |

`--token-file` reads the `token` field from a `webterm_mcp.json` sidecar (a local
convenience; the file holds `null` when the broker pins its token via env, in
which case pass the token directly). Token precedence is
`--token` > `$BROWSERLAND_MCP_TOKEN` > `$WEB_TERMINAL_MCP_TOKEN` > `--token-file`.

### Multi-host (#24)

One server process can front **several brokers** at once. Pass `--hosts` (or
`$BROWSERLAND_MCP_HOSTS`) a JSON array of `{name, url, token}` descriptors:

```bash
browserland-mcp --hosts '[
  {"name": "local",  "url": "http://127.0.0.1:4445",      "token": "abc…"},
  {"name": "remote", "url": "https://host2.ts.net:4445",  "token": "xyz…"}
]'
```

When `--hosts` is set it **supersedes** `--broker-url`/`--token`. Each `name`
must be unique and contain no `:` (it is the id separator). Window ids then
become **namespaced** strings `"<host>:<int>"` (e.g. `"local:4503601923583086"`),
and every id-taking tool routes on that prefix. A single `--broker-url`/`--token`
config is just one host named **`default`** — its ids look like `"default:12345"`.

> `BROWSERLAND_MCP_URL` is an `http://…` base URL and is **distinct** from the
> producer's `BROWSERLAND_BROKER_URL` (a `ws://…/browserland` URL) — different scheme and
> path, hence a separate name.

A missing token exits with a clear stderr message (no traceback).

## Run

```bash
# stdio MCP server, talking to the local broker
BROWSERLAND_MCP_TOKEN=… python -m webterm.mcptool
# or via the console script
BROWSERLAND_MCP_TOKEN=… browserland-mcp --broker-url http://127.0.0.1:4445
```

### Register with Claude Code

```bash
claude mcp add browserland \
  --env BROWSERLAND_MCP_TOKEN=… \
  --env BROWSERLAND_MCP_URL=http://127.0.0.1:4445 \
  -- python -m webterm.mcptool
```

(`-e` is the short form of `--env`.) Then the seven tools below are callable.

## Tools

Each tool maps to a broker endpoint and returns its JSON. Window `id`s are
namespaced `"<host>:<int>"` strings (see **Multi-host** above); the tools route
on the host prefix:

| Tool | Endpoint | Notes |
|---|---|---|
| `mcp_info(host?)` | `GET /mcp/info` | feature flags (`allow_launch`, `default_mode`) + broker `version`. Omit `host` → dict keyed by host name (per-host `{"error":…}` if unreachable) |
| `list_terminals` | `GET /mcp/terminals` | `{"terminals":[…], "errors":{host:msg}}`: all hosts merged, each terminal's `host` set to the config name (the broker's machine hostname preserved as `machine_host`) + namespaced `id`; a down host lands in `errors` without suppressing the rest. Each terminal carries a build `version` (agents also a `stale` flag) and `app_cursor` (cached DECCKM) |
| `list_profiles(host?)` | `GET /mcp/profiles` | launchable profile names + default. Omit `host` → dict keyed by host name |
| `read_screen(id, view?, lines?, wait_for_change?, wait_for_text?, wait_for_regex?, wait_absent?, timeout_ms?, since?)` | `POST /mcp/read` | screen rendered as a bounded plain-text grid (pyte, or a dependency-free fallback) + `alt_screen`/`cursor`/`content_hash`; `view="scrollback"` adds history. One wait mode (exclusive): `wait_for_change` holds until the hash changes (#26); `wait_for_text`/`wait_for_regex` (+`wait_absent` to invert) hold until that content appears/disappears and return `matched` (#51) — better on a busy TUI where the hash changes every frame. All bounded by `timeout_ms` (≤15000). `since=<prior content_hash>` requests a **delta** (#52): the reply drops `text` and returns `delta=true` + `changed_rows` (only the rows that differ) when the agent can diff it, else a full grid with `delta=false` |
| `send_input(id, data)` | `POST /mcp/input` | target window must be in **`readwrite`** mode |
| `send_keys(id, keys)` | `POST /mcp/input` | control/escape keys plain text can't express |
| `launch_terminal(profile?, cols=80, rows=24, title?, cwd?, host?)` | `POST /mcp/launch` | broker must have **`allow_launch`** enabled; `host` is required when multiple hosts are configured (optional with one). The returned `id` is namespaced |

Broker errors (`read_only`, `launch_disabled`, `mcp_disabled`, `auth_required`,
…) surface as a readable tool error (a `BrowserlandError`), not a raw stack
trace. Routing errors do too: a malformed id raises `malformed_id`, an id for an
unconfigured host raises `unknown_host`.

**`send_input` newline handling.** The tool maps newlines in `data` to a
carriage return (`\r`) — the byte a real Enter key sends — so a command actually
runs. This matters on PowerShell/PSReadLine, where a line-feed (`\n`) is only a
*soft line-continuation* and parks the line under a `>>` prompt instead of
submitting (issue #13); `\r` submits there and on a Unix shell alike. `\r\n`
collapses to one Enter, an explicit `\r` is untouched, and control/escape bytes
(Ctrl-C, ESC sequences) pass through. The mapping is **tool policy only**: the
`BrowserlandClient.send_input` method and the broker's `POST /mcp/input` endpoint
forward bytes **verbatim**, so a caller needing a literal LF or raw-mode input
drives the endpoint (or the client) directly.

**`send_keys` — control/escape keys.** `send_input` types literal text;
`send_keys(id, keys)` sends the byte sequences for keys that text can't express.
`keys` is a list of tokens: a named key (`Enter`, `Tab`, `Esc`, `Space`,
`Backspace`, `Delete`, `Up`/`Down`/`Left`/`Right`, `Home`, `End`, `PageUp`,
`PageDown`, `Insert`, `F1`–`F12`), a Ctrl chord `C-<char>` (`C-c` → `0x03`,
`C-Space` → NUL, `C-h` → `0x08`), an Alt chord `M-<char>` (ESC + char), or a
single literal character — e.g. `["C-c"]`, `["Esc"]`, `["Up","Up","Enter"]`. It
**emits the byte sequences** a keyboard would send; it does not synthesise OS
key events. Tokens go out verbatim (no newline→Enter rewrite). Whether `C-c`
interrupts depends on the target's PTY backend/mode.

**`send_keys` cursor keys (#23).** Arrows / Home / End are sent as SS3
(`ESC O x`) when the terminal has DECCKM (application-cursor-key mode) on — which
mc, vim, less and most full-screen TUIs enable — else as CSI (`ESC [ x`). When
the token list contains a cursor key, send_keys reads the terminal's **cached**
DECCKM from `list_terminals` (the agent pushes mode changes; no screen render),
and falls back to the CSI form if it can't read it (e.g. a non-agent producer).
Best-effort: a mode change racing the cache, or a producer that never reports
DECCKM, can still pick CSI. So `["Down"]` just moves the selection in mc without
the caller hand-assembling `ESC O B`.

**`read_screen` — screen vs scrollback (#21).** The result carries, besides
`text`/`cols`/`rows`: `alt_screen` (true for a full-screen TUI like mc/btop/vim —
the grid is the whole story, so there's no scrollback to chase), `cursor`
`{row, col}` 0-based within the grid (`null` on the rare `degraded` raw read),
`view` (the view actually produced), `history_lines`, and `app_cursor` (DECCKM,
informational here; send_keys reads the cached copy from list_terminals). For a
shell, pass
`view="scrollback"` with `lines=N` to prepend up to N lines of history above the
grid; `history_lines` reports how many were included (bounded by line count *and*
total cells). `alt_screen`/`app_cursor` are tracked live off the PTY stream, so
they stay correct even after a long-running TUI's mode-set has scrolled out of
the ring; when `alt_screen` is true, a scrollback request is answered with the
screen view.

> Best-effort note: the renderers don't model the alternate-screen buffer's
> save/restore, so in the brief moment *after* a TUI exits but *before* the shell
> repaints, the `screen` view may be blank/stale (the `alt_screen` flag is
> already correct). Scrollback returns lines that scrolled off the primary
> screen — it never includes a TUI's internal scrolling.

## Layout

| File | What |
|---|---|
| `client.py` | `BrowserlandClient` + `BrowserlandError` — the httpx client, one method per endpoint |
| `server.py` | FastMCP server; the seven `@mcp.tool()` functions + the host-routing helpers (`_route`, `_named_client`, `_aggregate`) |
| `__main__.py` | `python -m webterm.mcptool` — argparse, config resolution, `mcp.run()` |

Tests: `tests/test_mcptool.py` (skipped when `mcp` is absent).
