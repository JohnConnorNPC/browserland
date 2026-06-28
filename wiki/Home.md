# Browserland

Browserland is a web-based terminal desktop. Point a browser at the broker and you get a full windowed desktop of live terminals — tile them, tab them, split them, and drag them across virtual desktops. Each terminal is a real PTY running on some machine and streamed to the browser, so your shells keep running even when no browser is attached: close the tab, come back later, and the screen heals from a snapshot. Browserland also exposes an **MCP server** so AI harnesses can drive those terminals directly, including full-screen TUIs.

## How it fits together

Browserland has three pieces:

- **Agents** (producers) — headless processes that each own a real terminal, keep a buffer of recent output, and stream it over one WebSocket.
- **The broker** — a small web server. Agents register with it, browsers connect to it, it relays bytes both ways, serves the desktop UI, and can spawn new agents from pre-approved profiles.
- **The browser** — renders the desktop (a tiling window manager over xterm.js) and sends your keystrokes back to the PTY.

Because the PTY lives in the agent, your terminals survive browser reloads and broker restarts.

The default broker address is `http://127.0.0.1:4445/`.

## Where to start

New here? Read [[Getting-Started]] first. Once you are in the desktop, click the **"?" chip** at the bottom-right of the taskbar to open the in-app guide — it has a live search box covering every feature and your current keyboard shortcuts.

## Contents

### Basics

| Page | What it covers |
|---|---|
| [[Getting-Started]] | First steps: open the desktop, launch a terminal, find the help guide |
| [[Window-Modes]] | Floating vs. tiling, the tiling strip, switching modes |
| [[Keyboard-Shortcuts]] | The full shortcut table and how to rebind |

### Building layouts

| Page | What it covers |
|---|---|
| [[Arranging-Windows]] | Tabs, splits, stacks/rows, and the drag-to-merge drop zones |
| [[Columns-and-Widths]] | Tiling columns: width presets, moving and focusing columns |
| [[Snapping-and-Pop-out]] | The hold-to-snap and pop-out gestures, and the hold delay |
| [[Floating-Window-Controls]] | Move/resize, close/terminate/delete, pin, color, arrange-all, lock size |

### The desktop

| Page | What it covers |
|---|---|
| [[Workspaces]] | Virtual desktops and the pager |
| [[Taskbar]] | Taskbar items, the launch (+) menu, fullscreen, and clock |
| [[Context-Menus]] | A right-click reference across every surface |

### Content & windows

| Page | What it covers |
|---|---|
| [[Window-Types]] | Terminal, sticky note, text editor, file manager, task manager |

### Multi-host & AI

| Page | What it covers |
|---|---|
| [[Hosts-and-Multi-Browser]] | Remote hosts, status chips, the active-browser lease |
| [[MCP-and-AI-Agents]] | Per-window MCP access, enabling it per host, the tools overview |

## For developers

Building or running Browserland yourself? See the repo [README](https://github.com/JohnConnorNPC/browserland) and the [docs/TECHNICAL.md](https://github.com/JohnConnorNPC/browserland/blob/main/docs/TECHNICAL.md) reference.
