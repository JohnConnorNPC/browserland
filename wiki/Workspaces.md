Workspaces are virtual desktops. Each workspace is its own desktop of windows, so you can keep one set of terminals, notes, and editors on workspace 1 and a completely different set on workspace 2. Only one workspace is shown at a time.

Workspaces are a **mod**, enabled by default — see [[Hosts-and-Multi-Browser]] for how mods are turned on and off. Everything on this page works out of the box; *Turning workspaces off* at the bottom covers what happens if you disable it.

## Switching workspaces

You can move between workspaces a few ways:

- **Pager dots** — Click a dot in the pager at the bottom of the taskbar. Each dot is one workspace; the active one is highlighted.
- **The workspace menu** — Right-click the empty desktop, the empty tiling strip, or an empty part of the taskbar, and pick a workspace from the list. It works in both window modes, and because the taskbar is always on screen it stays reachable even when windows cover the desktop — so it still works with the pager hidden.
- **Previous / Next shortcuts** — Step to the workspace before or after the current one.
- **Go to workspace 1–5** — Jump straight to a numbered workspace.

| Action | Default binding |
|---|---|
| Previous workspace | `Ctrl+Alt+ArrowUp` |
| Next workspace | `Ctrl+Alt+ArrowDown` |
| Go to workspace 1 | `Ctrl+Alt+1` |
| Go to workspace 2 | `Ctrl+Alt+2` |
| Go to workspace 3 | `Ctrl+Alt+3` |
| Go to workspace 4 | `Ctrl+Alt+4` |
| Go to workspace 5 | `Ctrl+Alt+5` |

All of these are rebindable — see [[Keyboard-Shortcuts]].

Each row of the workspace menu shows that workspace's name and a count of what it holds — its columns in tiling mode, its floating windows in floating mode — with a `✓` on the one you are on. Below the list the menu adds, renames, and removes workspaces too; see the sections below. For more on the tiling strip, see [[Window-Modes]].

## Adding a workspace

There are a few ways to append a fresh, empty workspace:

- Click the **+** dot at the end of the pager.
- Choose **New workspace** from the pager-dot right-click menu.
- Choose **New workspace** from the workspace menu (right-click the empty desktop, strip, or taskbar).

## Sending a window to another workspace

Right-click a window's **title bar** to move it elsewhere (the [[Taskbar]]-item menu does not have these — it is only Focus / Minimize / Close / Terminate):

- **Send to workspace N** (or its name) — moves the window to that existing workspace.
- **Send to new workspace** — appends a new workspace and moves the window there.
- **On all workspaces** — keeps a **floating** window visible on every workspace. (The menu shows it as `Show on all workspaces`, with a `✓ On all workspaces` once it is enabled.)

The **Send to** items appear only on **tiled** windows' title-bar menus. A floating window's menu offers **Tile this window** instead — tile it first, then send it — or use **Show on all workspaces** to make it visible everywhere.

## Where a window opens, and what happens when you launch it again

A window you open lands on the workspace you are **currently on**, and it stays there — it never appears for a moment and then disappears.

Launching something that is **already open** does not open a second copy. What happens next depends on how that window is arranged:

| The open window is… | Launching it again… |
|---|---|
| **Floating**, on another workspace | Moves it to the workspace you are on, and focuses it. |
| **Floating**, set to *On all workspaces* | Focuses it where it is — it is already visible everywhere, so it is never moved. |
| **Tiled**, on another workspace | Takes **you** to its workspace and focuses it there. |
| Minimized | Restores it, applying the same rule. |

The split is deliberate. A floating window can be picked up and moved anywhere, so it comes to you. A tiled window's place *is* its column in that workspace's strip, so moving it would rearrange the layout you built — instead you are taken to it. To move a tiled window for real, use **Send to workspace N** from its title-bar menu.

This applies to every way of launching: the **+** menu, the keyboard shortcut, and the app's own button or chip.

Clicking a window's **taskbar** item is different: it always takes you to the window's workspace, floating or tiled (a window on all workspaces stays where it is), then focuses it, un-minimizing it if needed. Clicking the item of the window you are already focused on minimizes it.

**Closing a window forgets its workspace.** Reopen it later and it opens on whichever workspace you are on, like anything else newly opened. Reloading the page is different: nothing is closed, so every window comes back on the workspace it was on.

## Renaming, removing, and labeling workspaces

Right-click a **pager dot** for that workspace's options:

| Menu item | What it does |
|---|---|
| `Rename…` | Give the workspace a custom name. |
| `Remove workspace` | Delete that workspace (disabled when only one workspace remains). |
| `Show names` | Show each workspace's name on its pager dot. |
| `Show numbers` | Show workspace numbers on the dots instead. |
| `New workspace` | Append a fresh empty workspace. |

`Show names` / `Show numbers` is a single toggle for how *all* dots are labeled; the active choice is marked with a `✓`. It is also **Workspace labels** under **Control Panel → Windows → Workspaces**.

The workspace menu — right-click the empty desktop, the empty tiling strip, or an empty part of the taskbar — renames and removes too, naming its target so there is no doubt which workspace it acts on:

| Menu item | What it does |
|---|---|
| `Rename <workspace>…` | Rename the workspace you are on. |
| `Remove <workspace>` | Delete the workspace you are on (disabled when only one remains). |

These are the same two actions as the pager-dot menu, on a surface that does not depend on the pager — so they still work with the pager hidden.

## Hover preview

Hovering a pager dot pops up a small schematic of that workspace: its columns and rows drawn to scale and labelled with each window's title, a `(+N)` badge on a tabbed tile counting its hidden tabs, and a count of the workspace's floating windows underneath. It shows exactly what the strip would show — a minimized or disconnected window is left out of the preview the same way it is left out of the strip.

## Taskbar behaviour

Chips for windows on other workspaces are dimmed and carry a small badge naming their workspace, so the bar always indicates where you are. The mod's three settings all live together under **Control Panel → Windows → Workspaces** — the mod places them beside the other window settings rather than in the shared **Mods** applet, each carrying its mod badge:

| Setting | What it does |
|---|---|
| **Workspace labels** | Numbers or names on the pager dots — the Control Panel twin of the pager dot menu's `Show names` / `Show numbers`. |
| **Hide taskbar items from other workspaces** | Leave out chips for windows on other workspaces entirely, instead of dimming them. |
| **Hide the workspace pager from the taskbar** | Remove the pager dots from the taskbar and give the space back. Off by default. |

Clicking a chip for a window on another workspace switches there first, so a chip never does nothing — including a chip for a session that is currently closed.

### Hiding the pager

If you switch workspaces from the keyboard, **Hide the workspace pager from the taskbar** reclaims the space the dots take up. It hides the pager and nothing else — the badges and dimming, the chip-click workspace switch, **Send to workspace**, floating-window masking, and the seven shortcuts all keep working exactly as before.

Everything the dots could do is still reachable: the workspace menu (right-click the empty desktop, the empty tiling strip, or an empty part of the taskbar) switches, adds, renames, and removes workspaces, and the shortcuts above switch. Turn the setting back off and the pager returns with its workspaces unchanged.

Like the other two, this setting is shown on the local broker's tab and applies to every browser viewing this broker: it is saved with the broker that serves the page (its /state), not per host.

## Workspaces over MCP

An MCP agent can manage workspaces too — with two gates stacked on top of each other, because workspace changes span **every** window on the desktop, whatever its scope:

- the broker's **Allow MCP to arrange the desktop** switch (`allow_desktop`) must be on — it gates the whole desktop relay, window arranging included; and
- the agent must hold the **admin credential** (#245) — a plain scoped or unscoped agent never gets to rename or remove a workspace out from under windows it cannot even see.

The surface is the `workspaces` tool of the shipped MCP server (see [[MCP-and-AI-Agents]]): `list`, `add` (optional name, trimmed to 40 characters), `rename` (index + name; a blank name clears it), `switch` (index), `send-window` (a window id + a target index), and `remove` (index). Semantics follow the desktop's own rules, with one deliberate difference:

- **Removing a non-empty workspace never pops a dialog.** Over MCP the answer is `needs_confirm` carrying the number of **live** windows the workspace holds; the agent passes `force: true` to proceed, and the windows move to the **adjacent** workspace — exactly what the desktop's own confirmation dialog says happens. The last workspace is refused outright (`last_workspace`), force or not.
- **Mod off or absent.** With the workspaces mod disabled every op answers `inactive`; with it not installed, `absent` — a state report, never a silent no-op. (Over MCP you also learn *which* desktop you missed: workspace data belongs to a browser's **home** broker, and an op relayed through any other broker answers `not_home`.)
- Everything else behaves as the desktop does: adding switches to the new workspace, `send-window` re-stamps the window's column onto the target workspace, and `list` reports the workspaces, the active index and where each column lives.

## What is shared, and what is per browser

The set of workspaces, their names, and which columns belong to each are part of the broker's shared layout, so every browser looking at that broker sees the same workspaces. Which workspace a **floating** window belongs to is per browser, because a floating window's pixel geometry is per browser too.

## Turning workspaces off

Disabling the mod is a different thing from hiding the pager, and much bigger. **Hide the workspace pager from the taskbar** (above) removes the dots and leaves everything else running; disabling the mod removes the feature.

Nothing is destroyed and nothing is hidden either way. Every tiled column lives on the one desktop the tiling core owns — workspaces only decide which of them the strip draws. Disable the mod (Control Panel → Mods) and:

- every column from every workspace appears together on a single desktop;
- the pager disappears;
- the seven workspace shortcuts leave the Keyboard shortcuts list (your bindings are remembered, not deleted);
- **Send to workspace** and **On all workspaces** leave the title-bar menus, and the whole workspace block leaves the empty-desktop / taskbar menu;
- the mod's three settings leave **Control Panel → Windows**;
- any floating window that was masked to another workspace becomes visible.

Re-enable it and your workspaces come back exactly as they were. A column created while the mod was off joins whichever workspace is active when you turn it back on.

One thing to know if you run **two browsers on the same broker with the mod on in one and off in the other**: the browser without it sees every column on one desktop, so rearranging them there rearranges your workspaces too. Stacking a window from workspace 1 into a column that belongs to workspace 2 really does move it to workspace 2 — the column is the membership. That is the same thing happening either way round; it just isn't labelled as a workspace move in the browser that can't see workspaces.

## Related pages

- [[Taskbar]] — where the pager and its dots live.
- [[Context-Menus]] — the full right-click reference, including pager dots, windows, and the empty desktop.
- [[Keyboard-Shortcuts]] — view and rebind the workspace shortcuts.
