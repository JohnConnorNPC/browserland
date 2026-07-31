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
- **The launch button (+)** — by default, left-click the **+** at the left of the taskbar to launch a default terminal on your default host (the local broker unless you pick another under **Control Panel → Hosts** — see [[Hosts-and-Multi-Browser]]), and right-click it for the full profile / app menu (other profiles, app windows, and "Open in folder…"). If you have added remote hosts, each broker gets a live status row with its profiles below it — and the taskbar's broker badge opens this same menu. A Control Panel toggle can swap these two gestures (see [[Taskbar]]).
- **The "New terminal" shortcut** — press `Ctrl+Alt+Enter`.

The shortcut always starts on the **local broker**; the quick-launch gesture follows the default-host setting. To start a terminal on any other host, pick its profile from the launch menu. Each terminal is a real shell on its broker's host. Type in it like any terminal; close the window later and the shell keeps running so you can reattach.

From the **+** menu you can also open other window types — a sticky note, a text editor, a file manager, a task manager, a scratchpad, and the session recorder. If you enable the **AI status** mod (Control Panel → Mods; it ships off by default), an **AI-provider status** monitor joins the menu too — enabling it lets the broker check each provider's public status page, so the broker's egress IP becomes visible to those hosts. The **Update check** mod adds an **Update check** window reporting whether this build is current with upstream; it also ships off, and the broker will not reach GitHub until an operator sets `update_check_enabled` in its config. See [[Window-Types]] for what each one does, and [[Taskbar]] for the rest of the launch menu.

## The Control Panel

The **Control Panel** is where you configure the desktop. Open it any of these ways:

- **Right-click the launch (+) button** and choose **Control panel** (that menu uses SVG icons, not emoji).
- **Right-click the empty desktop or the taskbar** and choose **🎛 Control panel**.
- Press the **Open control panel** shortcut, `Ctrl+Alt+p`.

It opens as a moveable floating window — drag its title bar, resize it, or minimize it like any other window — with a tab per connected broker.

### Applets

Inside a broker's tab the settings are grouped into **applets**: the panel opens on a grid of captioned icons, and you pick a topic first. Clicking one (or pressing Enter on a focused icon) swaps the grid for that applet's settings plus a **Back** button. It is one level deep — there is no nesting. The line under the grid describes whichever icon you are hovering or have tabbed to.

| Applet | What lives there |
|---|---|
| **Desktop** | Theme, background pattern, the clock chip's time zone, the start button's label and gestures, the broker status chip — and the terminal font, via an opt-in mod (off by default; enable it under **Control Panel → Mods**) |
| **Windows** | Window size, window mode (floating vs. tiling — see [[Window-Modes]]), taskbar / title labels, drag hold delay (see [[Snapping-and-Pop-out]]), window slide speed, the workspace scrollbar, the terminal close button, and the Workspaces settings (see [[Workspaces]]) |
| **Input** | Keybindings (see [[Keyboard-Shortcuts]]) and Clipboard (OSC 52) — whether programs on that host may set your clipboard, off by default |
| **Terminals** | The default start path, the default terminal profile, and the launch profiles themselves |
| **Startup** | Restore-on-refresh |
| **Access** | MCP — AI-agent access to your terminals (see [[MCP-and-AI-Agents]]) |
| **Mods** | Which mods this browser runs — and, since it is no longer enable/disable only, **Install a mod…** and **Uninstall**, a shipped-vs-installed badge, and an install preview with advisory warnings (see [[Installing-Mods]]). Plus the settings each enabled mod contributes. A mod may also place its control in one of the applets above — the Color scheme radio sits in **Desktop**, for instance — and a mod-owned setting carries a small badge naming its mod wherever it lands |
| **Advanced** | **Mods on this broker**: which mods that broker pins on or off for every browser that loads its page — including a remote broker's, from its own tab (see [[Hosts-and-Multi-Browser]]) |

An applet only appears when it has something to show on the tab you are on. A remote broker's tab has no **Desktop** or **Startup** icon, for example, because everything in them belongs to the browser you are sitting at rather than to that broker (see *Per-browser vs. per-host settings* below).

The **Browser** tab is not an applet grid — it is short already, and holds the Hosts list, **Broker registry**, **Sync mods** (copy this broker's mod setup to the other brokers you have configured, instead of repeating it on each machine — see [[Hosts-and-Multi-Browser]]), and Troubleshooting.

### Finding a setting

Two ways in, and you do not have to guess which applet something is under:

- **The box above the grid** filters as you type. It matches section headings, control labels, hints and the values you have entered, and icons with no match drop out of the grid. When what you typed narrows to a single applet, press **Enter** to open it with the matching setting scrolled into view; **Esc** clears the box.
- **Show everything** turns the applets off and gives you the whole flat list in one scroll — the browse path, the `Ctrl+F` path, and the escape hatch for when the grouping puts something where you did not look. Filtering while it is on filters that flat list instead of the grid. The toggle is remembered in the browser you set it from and is never shared with anyone else viewing the same broker.

### Per-browser vs. per-host settings

- **Browser-global settings** follow the browser you are sitting at — theme and background, the terminal font (an opt-in mod, off by default — turn it on under **Control Panel → Mods**), the start-button label, restore-on-refresh, when the broker status chip shows, the workspace pager's label mode, visibility and taskbar filter, and the clock chip's time zone.
- **Per-host settings** live on a tab per broker — window mode, drag hold delay, MCP, keyboard shortcuts, the default terminal profile, and the default start path — so each broker remembers its own. **Mods on this broker** sits there too, and is the one setting that governs *other* browsers: it pins mods on or off for everyone who loads that broker's page, so it overrides their per-browser Mods list rather than yours.
- **Clipboard (OSC 52)** is per host like the settings above it, but is the one setting that is also **browser-local**: it never travels to a broker, in either direction. That is deliberate. It is the switch that decides whether a broker may write to your clipboard, so storing it on that broker would let the broker grant itself the permission. Turning it on for one host has no effect on any other, and it does not follow you to another browser.

<!-- help:ignore-start -->
<!-- Cross-nav to other wiki pages — in-app Help navigates via its section rail,
     not page links, so this is excluded from the in-app guide (GitHub-only). -->
## Next steps

- [[Window-Modes]] — floating vs. tiling, the tiling strip, and switching between them.
- [[Arranging-Windows]] — tab, split, and stack windows; the drag-to-merge drop-zone cheat sheet.
- [[Taskbar]] — taskbar items, the launch (+) menu, fullscreen, and the clock.
- [[Keyboard-Shortcuts]] — the full shortcut table and how to rebind keys (including **Toggle help**).
<!-- help:ignore-end -->
