Browserland is a web-based terminal desktop: you point a browser at the **broker** and get a full windowed desktop of live terminals and app windows. This page walks you through your first session.

## Open the broker

Open the broker URL in your browser. By default that is:

```
http://127.0.0.1:4445/
```

When the page loads you get the desktop: an empty work area with a **taskbar** along the bottom. The shells that Browserland runs keep running even when no browser is attached, so if terminals were started earlier they are still there waiting for you — reopen the tab and the screen heals from a snapshot.

## Launch your first terminal

You have three quick ways to get a terminal on screen:

- **Click an existing session** — if any terminals are already running, their buttons appear in the taskbar. Click one to focus and raise it (or restore it if it was minimized).
- **The launch button (+)** — left-click the **+** at the left of the taskbar to launch the local broker's default terminal. Right-click it for the full profile / app menu (other profiles, app windows, and "Open in folder…"). If you have added remote hosts, the right-click menu groups profiles per host.
- **The "New terminal" shortcut** — press `Ctrl+Alt+Enter`.

Left-click and the shortcut always launch on the **local broker**; to start a terminal on a remote host, pick its profile from the right-click menu. Each terminal is a real shell on its broker's host. Type in it like any terminal; close the window later and the shell keeps running so you can reattach.

From the **+** menu you can also open other window types — a sticky note, a text editor, a file manager, and a task manager. If you enable the **AI status** mod (Control Panel → Mods; it ships off by default), an **AI-provider status** monitor joins the menu too — enabling it lets the broker check each provider's public status page, so the broker's egress IP becomes visible to those hosts. See [[Window-Types]] for what each one does, and [[Taskbar]] for the rest of the launch menu.

## The in-app interface guide (the "?" chip)

Browserland ships a searchable guide built into the app. Open it from the **"?" chip** at the bottom-right of the taskbar.

- Type in the search box to filter every entry live.
- The guide covers layout modes, snapping, tabs, splits, workspaces, the taskbar, MCP access, and more — the same topics as this wiki, available without leaving the desktop.

You can also bind a key to open it: the guide is the **Toggle help** action, which is **unbound by default**. Assign a combo under **Control Panel → Keyboard shortcuts** (see [[Keyboard-Shortcuts]]).

## The Control Panel

The **Control Panel** is where you configure the desktop. Open it any of these ways:

- **Right-click the launch (+) button** and choose **🎛 Control panel**.
- **Right-click the empty desktop or the taskbar** and choose **🎛 Control panel**.
- Press the **Open control panel** shortcut, `Ctrl+Alt+p`.

It opens as a moveable floating window — drag its title bar, resize it, or minimize it like any other window — with a tab per connected broker. Among the settings it covers (not an exhaustive list):

| Setting | What it controls |
|---|---|
| Appearance | Theme, background pattern, terminal font |
| Window mode | Floating vs. tiling (see [[Window-Modes]]) |
| Drag hold delay | The hold time for the snap and pop-out gestures (see [[Snapping-and-Pop-out]]) |
| Hosts | Remote brokers you connect to (see [[Hosts-and-Multi-Browser]]) |
| MCP | AI-agent access to your terminals (see [[MCP-and-AI-Agents]]) |
| Keyboard shortcuts | Rebind any action (see [[Keyboard-Shortcuts]]) |
| Start button / taskbar | The + button's label, the taskbar workspace filter, restore-on-refresh |

### Per-browser vs. per-host settings

- **Browser-global settings** follow the browser you are sitting at — theme and background, terminal font, the start-button label, restore-on-refresh, the taskbar workspace filter, and the clock chip's time zone.
- **Per-host settings** live on a tab per broker — window mode, drag hold delay, MCP, keyboard shortcuts, the default terminal profile, and the default start path — so each broker remembers its own.

<!-- help:ignore-start -->
<!-- Cross-nav to other wiki pages — in-app Help navigates via its section rail,
     not page links, so this is excluded from the in-app guide (GitHub-only). -->
## Next steps

- [[Window-Modes]] — floating vs. tiling, the tiling strip, and switching between them.
- [[Arranging-Windows]] — tab, split, and stack windows; the drag-to-merge drop-zone cheat sheet.
- [[Taskbar]] — taskbar items, the launch (+) menu, fullscreen, and the clock.
- [[Keyboard-Shortcuts]] — the full shortcut table and how to rebind keys (including **Toggle help**).
<!-- help:ignore-end -->
