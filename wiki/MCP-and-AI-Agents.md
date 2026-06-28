Browserland can let an AI harness (an MCP client) **list, observe, drive, and launch** your terminals — including full-screen TUIs — over a token-gated interface. Nothing is reachable until you turn it on, and you stay in control: access is granted per host and per window, and the broker is always the sole authority.

This page covers MCP access from your point of view at the desktop. For the full HTTP contract and setup details, follow the [reference links](#reference) at the bottom.

## What MCP access means

An MCP client (such as an AI coding agent) connects to your broker and can attach to the exact terminals you are working in. Because each terminal is a real PTY that survives reloads, the agent's handle to a session stays valid across browser reloads and broker restarts.

The agent can only see and touch the terminals you allow. There are three layers of control, all opt-in:

1. **Master enable** for the host (Control Panel → MCP).
2. **Per-window access** — `off`, `read`, or `read-write` — set on each terminal.
3. A separate gate for whether the agent may **launch new terminals**.

## Per-window access (the robot button)

Every terminal window has a **robot button** that sets that window's MCP access for agents:

| Mode | What an agent can do |
|---|---|
| `off` | Nothing — the window is hidden from MCP entirely |
| `read` | Observe only — read the current screen |
| `read-write` | Observe **and** type into the terminal (`send_input`) |

When an agent reads the screen or types into a terminal, that window's **robot icon briefly flashes**, so you can see at a glance when a harness is touching a session.

You can also set per-window access from a window's right-click menu (see [[Context-Menus]]). New windows start in the host's global **default mode** (configured below).

The robot button is specific to **terminals**. For the full list of window types, see [[Window-Types]].

## Enable MCP for a host

MCP is configured per host in **Control Panel → MCP**:

- **Enable MCP access** — the master switch for that host. While it is off, every MCP call is refused.
- **default mode** — the per-window access (`off` / `read` / `read-write`) that new terminals start with.
- **Allow MCP to launch terminals** — an optional, separate gate that lets agents create new terminals (not just drive existing ones).
- **token** — a secret that authenticates the agent (set it yourself or hit **generate**). This is a **bearer secret distinct from the browser/UI password** you use to log in; granting MCP access does not hand out your login.

> Settings are per host, so each broker you add has its own MCP switch, default mode, launch gate, and token. See [[Hosts-and-Multi-Browser]] for adding and managing hosts.

The in-app guide also shows a live **MCP status (this host)** entry — whether MCP is enabled, the default mode for new windows, and whether launch-via-MCP is allowed.

## Safety model

Access is layered and **off by default** — you opt in at every level:

- **Master enable is OFF by default.** While it is off, every MCP call returns `403 mcp_disabled`.
- **Per-window mode** is `off` / `read` / `readwrite`, with a global `default_mode` for new windows. `off` hides the window, `read` allows observation, and `readwrite` additionally allows typing.
- **`allow_launch`** is a separate gate — turning MCP on does not, by itself, let agents spawn terminals.
- **The MCP token** is a separate bearer secret from your browser auth/UI password.

The agents are just producers; the broker stays the sole authority and gates every call by these rules.

## Tools overview

An MCP client sees these tools, each mapping to a broker endpoint:

| Tool | What it does |
|---|---|
| `mcp_info(host?)` | Reports feature flags (`allow_launch`, `default_mode`) |
| `list_terminals` | Lists all running sessions across hosts (id, title, cwd, agent, kind, cols/rows, mode) |
| `list_profiles(host?)` | Lists launchable profile names and the default |
| `read_screen(id)` | Returns the terminal's current screen as a bounded plain-text grid |
| `send_input(id, data)` | Types text into a window (window must be `read-write`; newlines are sent as Enter) |
| `send_keys(id, keys)` | Sends control/escape keys, e.g. `["C-c"]`, `["Esc"]`, `["Up","Enter"]` |
| `launch_terminal(profile?, cols, rows, title?, cwd?, host?)` | Launches a new terminal (broker must have `allow_launch` enabled) |

### Window ids

Window ids are namespaced as `"<host>:<int>"` — for example `"default:12345"`. The host part matches the name of the host the terminal belongs to, so one MCP server can front several brokers at once. See [[Hosts-and-Multi-Browser]] for how hosts are named.

## Reference

This page is the user-facing summary; it does not duplicate the full HTTP contract, error table, or config sidecar. For those, see:

- README → **MCP & AI agent access**: <https://github.com/JohnConnorNPC/browserland/blob/main/README.md#mcp--ai-agent-access>
- Technical reference: <https://github.com/JohnConnorNPC/browserland/blob/main/docs/TECHNICAL.md>
- The shipped stdio MCP server (`webterm.mcptool`): <https://github.com/JohnConnorNPC/browserland/blob/main/webterm/mcptool/README.md>
