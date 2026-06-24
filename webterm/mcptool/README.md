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

`--token-file` reads the `token` field from a `webterm_mcp.json` sidecar (a local
convenience; the file holds `null` when the broker pins its token via env, in
which case pass the token directly). Token precedence is
`--token` > `$BROWSERLAND_MCP_TOKEN` > `$WEB_TERMINAL_MCP_TOKEN` > `--token-file`.

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

(`-e` is the short form of `--env`.) Then the six tools below are callable.

## Tools

Each tool maps 1:1 to a broker endpoint and returns its JSON verbatim:

| Tool | Endpoint | Notes |
|---|---|---|
| `mcp_info` | `GET /mcp/info` | feature flags (`allow_launch`, `default_mode`) |
| `list_terminals` | `GET /mcp/terminals` | visible terminals (windows in `off` mode are hidden) |
| `list_profiles` | `GET /mcp/profiles` | launchable profile names + default |
| `read_screen(id)` | `POST /mcp/read` | screen rendered as plain text |
| `send_input(id, data)` | `POST /mcp/input` | target window must be in **`readwrite`** mode |
| `launch_terminal(profile?, cols=80, rows=24, title?, cwd?)` | `POST /mcp/launch` | broker must have **`allow_launch`** enabled |

Broker errors (`read_only`, `launch_disabled`, `mcp_disabled`, `auth_required`,
…) surface as a readable tool error (a `BrowserlandError`), not a raw stack trace.

## Layout

| File | What |
|---|---|
| `client.py` | `BrowserlandClient` + `BrowserlandError` — the httpx client, one method per endpoint |
| `server.py` | FastMCP server; the six `@mcp.tool()` functions |
| `__main__.py` | `python -m webterm.mcptool` — argparse, config resolution, `mcp.run()` |

Tests: `tests/test_mcptool.py` (skipped when `mcp` is absent).
