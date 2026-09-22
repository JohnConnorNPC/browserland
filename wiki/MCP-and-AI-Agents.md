Browserland can let an AI harness (an MCP client) **list, observe, drive, and launch** your terminals — including full-screen TUIs — over a token-gated interface. Nothing is reachable until you turn it on, and you stay in control: access is granted per host and per window, and the broker is always the sole authority.

This page covers MCP access from your point of view at the desktop. For the full HTTP contract and setup details, follow the [reference links](#reference) at the bottom.

## What MCP access means

An MCP client (such as an AI coding agent) connects to your broker and can attach to the exact terminals you are working in. Because each terminal is a real PTY that survives reloads, the agent's handle to a session stays valid across browser reloads and broker restarts.

The agent can only see and touch the terminals you allow. There are three layers of control, all opt-in:

1. **Master enable** for the host (Control Panel → Access → MCP access).
2. **Per-window access** — `off`, `read`, or `readwrite` — set on each terminal.
3. A separate gate for whether the agent may **launch new terminals**.

## Per-window access (the robot button)

Every terminal window has a **robot button** that sets that window's MCP access for agents:

| Mode | What an agent can do |
|---|---|
| `off` | Nothing — the window is hidden from MCP entirely |
| `read` | Observe only — read the current screen |
| `readwrite` | Observe **and** type into the terminal (`send_input`) — shown as "Read-write" in the UI |

When an agent reads the screen or types into a terminal, that window's **robot icon briefly flashes**, so you can see at a glance when a harness is touching a session.

You can also set per-window access from a window's right-click menu (see [[Context-Menus]]). New windows start in the host's global **default mode** (configured below).

On a broker that supports scopes, the robot button also holds the window's **scope**: see **Scopes** below.

The robot button is specific to **terminals**. For the full list of window types, see [[Window-Types]].

## Enable MCP for a host

MCP is configured per host in **Control Panel → Access → MCP access**:

- **Enable MCP access** — the master switch for that host. While it is off, every MCP call is refused.
- **default mode** — the per-window access (`off` / `read` / `readwrite`) that new terminals start with.
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

## Scopes

Several MCP clients often share one broker: for example a "manager" and a "worker" Claude Code session in one project folder, and another pair in a second folder. Each folder's `.mcp.json` starts its own MCP server. A **scope** keeps the pairs apart: a server that declares scope `projA` sees and drives only the windows tagged `projA`.

- **What a scope is.** A name of 1–64 characters from `A-Z a-z 0-9 . _ -`, starting with a letter or digit. A window carries at most one.
- **Who sees what.** A client that declares a scope sees only the windows tagged with it, each still only as far as its access mode allows. A client that declares **no** scope sees every window its mode allows, tagged or not: that is the admin view, and any `.mcp.json` without a scope gets it. An untagged window is hidden from every scoped client.
- **Where a client's scope is set.** On the MCP server: `--scope`, or the `BROWSERLAND_MCP_SCOPE` environment variable (the flag wins, and an empty value means no scope), or a `"scope"` on one host of `BROWSERLAND_MCP_HOSTS`. The easiest way is the **Copy .mcp.json** button (below).
- **How a window gets its tag.** A window launched by a scoped client (`launch_terminal`) is tagged before it starts and comes up in **Read-write** unless the launch asks for another `mode`, so its launcher can drive it at once. A window you started yourself has no tag: give it one from its robot button (type a scope in the **Scope** field and press Enter; Escape cancels; an empty field removes the tag) or from its title-bar right-click menu (**MCP scope**, see [[Context-Menus]]). A scoped client cannot see or find an untagged window until you tag it; that is by design. Both controls appear only on a broker that supports scopes.
- **Seeing it.** A tagged window's taskbar button shows the scope as a small blue badge, its tooltip names it (`scope <name>`), and so does the robot button's tooltip.
- **It is kept.** The broker stores each window's scope and its MCP access mode (in `webterm_mcp_windows.json`), so both survive a broker restart and an agent reconnect: the per-window mode no longer resets when the broker restarts. They are re-applied only to the same process on the same host: when a window id is reused by a different process, that window starts untagged at the default mode. A stored entry whose window is gone is dropped after 7 days.
- **A convention, not security.** Every client holds the same MCP token and can declare any scope, or none. A scope keeps well-behaved clients out of each other's windows; it does not stop one that wants to look.
- **Profiles are global.** Any client may launch any profile; a scoped client's launch lands in its own scope.
- **Old brokers and proxies fail closed.** Before its first call to a host, a scoped MCP server checks that the broker echoes its scope back. An older broker without scope support, or a reverse proxy that drops or rewrites the `X-Browserland-Scope` header, makes every call to that host fail with `scope_unsupported` rather than show every window. A proxy must pass the header through, once. An empty header value counts as no scope.

### Copy .mcp.json

**Control Panel → Access → MCP access** has a **scope** field and a **Copy .mcp.json** button. The button copies a ready-to-save `.mcp.json` for that host: the broker's own Python as the command, a `PYTHONPATH` naming the broker's own Browserland folder (so the server it starts is the one that understands scopes, not an older copy elsewhere on the machine), the broker's URL (a loopback one for the broker serving this page, the stored address for any other host), the MCP token and, if you typed one, the scope. Save it as `.mcp.json` in the project folder and start the MCP client there. The button stays off until MCP has a token.

- The file contains the MCP token, a bearer secret: keep `.mcp.json` out of git.
- It names absolute paths on the broker's machine (its Python and its Browserland folder), which the Control Panel shows only to someone already signed in.
- `command`, `PYTHONPATH` and `url` are the broker machine's own: edit them for an MCP client on another machine. For the broker serving this page the URL is `http://127.0.0.1:<port>`; a broker listening only on a non-loopback address needs that address instead.
- If the browser refuses the clipboard, the text opens in a dialog so you can copy it by hand.

## Tools overview

An MCP client sees these tools, each mapping to a broker endpoint:

| Tool | What it does |
|---|---|
| `mcp_info(host?)` | Reports feature flags (`allow_launch`, `default_mode`) |
| `list_terminals` | Lists all running sessions across hosts (id, title, cwd, agent, kind, cols/rows, mode, scope); a scoped server lists only its scope's windows |
| `list_profiles(host?)` | Lists launchable profile names and the default |
| `read_screen(id, …)` | Returns the terminal's screen. Far more than a bounded grid: `view="scrollback"` and `lines` widen what is returned, `since` returns only what is new, `attrs` adds styling runs, and it can **wait** rather than poll — `wait_for_change`, `wait_for_text`, `wait_for_regex`, `wait_absent`, `wait_for_idle` |
| `send_input(id, data)` | Types text into a window (window must be `readwrite`; newlines are sent as Enter) |
| `send_keys(id, keys, delay_ms?)` | Sends control/escape keys, e.g. `["C-c"]`, `["Esc"]`, `["Up","Enter"]`. Also accepts `S-` (shift) modifiers and a bare `LF` token, and `delay_ms` paces the keystrokes |
| `set_pace(id, pace_ms)` | Sets a per-terminal default inter-key pacing for `send_keys` (window must be `readwrite`; ephemeral, `0` disables, capped at 1000 ms) |
| `reset_terminal(id)` | Wipes Browserland's screen buffer for the window so the next `read_screen` starts clean (window must be `readwrite`; does not touch the running app) |
| `flush_input(id)` | Discards keystrokes queued to the app but not yet consumed (window must be `readwrite`; a no-op on a Windows/ConPTY agent) |
| `launch_terminal(profile?, cols, rows, title?, cwd?, host?, mode?)` | Launches a new terminal (broker must have `allow_launch` enabled). A scoped server's launch is tagged with its scope and starts in `readwrite`, or in `mode` when given |

### Window ids

Window ids are namespaced as `"<host>:<int>"` — for example `"default:12345"`. The host part matches the name of the host the terminal belongs to, so one MCP server can front several brokers at once. See [[Hosts-and-Multi-Browser]] for how hosts are named.

<!-- help:ignore-start -->
<!-- External-link cross-nav (README / TECHNICAL / mcptool) — not useful inside
     the in-app guide, which is the desktop's own help. GitHub-only. -->
## Reference

This page is the user-facing summary; it does not duplicate the full HTTP contract, error table, or config sidecar. For those, see:

- README → **MCP & AI agent access**: <https://github.com/JohnConnorNPC/browserland/blob/main/README.md#mcp--ai-agent-access>
- Technical reference: [[Technical Reference|Technical-Reference]]
- The shipped stdio MCP server (`webterm.mcptool`): <https://github.com/JohnConnorNPC/browserland/blob/main/webterm/mcptool/README.md>
<!-- help:ignore-end -->
