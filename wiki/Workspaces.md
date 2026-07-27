Workspaces are virtual desktops. Each workspace is its own desktop of windows, so you can keep one set of terminals, notes, and editors on workspace 1 and a completely different set on workspace 2. Only one workspace is shown at a time.

Workspaces are a **mod**, enabled by default — see [[Hosts-and-Multi-Browser]] for how mods are turned on and off. Everything on this page works out of the box; *Turning workspaces off* at the bottom covers what happens if you disable it.

## Switching workspaces

You can move between workspaces a few ways:

- **Pager dots** — Click a dot in the pager at the bottom of the taskbar. Each dot is one workspace; the active one is highlighted.
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

In tiling mode, you can also right-click the empty strip or desktop. The menu lists every workspace (with its column count) so you can jump to one, plus a **New workspace** item. For more on the tiling strip, see [[Window-Modes]].

## Adding a workspace

There are a few ways to append a fresh, empty workspace:

- Click the **+** dot at the end of the pager.
- Choose **New workspace** from the pager-dot right-click menu.
- In tiling mode, choose **New workspace** from the empty-strip / empty-desktop menu.

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

Clicking a window's **taskbar** item behaves the same way, with one addition — it also un-minimizes, and clicking the item of the window you are already focused on minimizes it.

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

`Show names` / `Show numbers` is a single toggle for how *all* dots are labeled; the active choice is marked with a `✓`.

## Hover preview

Hovering a pager dot pops up a small schematic of that workspace: its columns and rows drawn to scale and labelled with each window's title, a `(+N)` badge on a tabbed tile counting its hidden tabs, and a count of the workspace's floating windows underneath. It shows exactly what the strip would show — a minimized or disconnected window is left out of the preview the same way it is left out of the strip.

## Taskbar behaviour

Chips for windows on other workspaces are dimmed and carry a small badge naming their workspace, so the bar always indicates where you are. **Control Panel → Mods → Hide taskbar items from other workspaces** hides them outright instead of dimming. **Workspace labels** in the same place is the Control Panel twin of the pager dot's `Show names` / `Show numbers`.

Clicking a chip for a window on another workspace switches there first, so a chip never does nothing — including a chip for a session that is currently closed.

## What is shared, and what is per browser

The set of workspaces, their names, and which columns belong to each are part of the broker's shared layout, so every browser looking at that broker sees the same workspaces. Which workspace a **floating** window belongs to is per browser, because a floating window's pixel geometry is per browser too.

## Turning workspaces off

Nothing is destroyed and nothing is hidden. Every tiled column lives on the one desktop the tiling core owns — workspaces only decide which of them the strip draws. Disable the mod (Control Panel → Mods) and:

- every column from every workspace appears together on a single desktop;
- the pager disappears;
- the seven workspace shortcuts leave the Keyboard shortcuts list (your bindings are remembered, not deleted);
- **Send to workspace** and **On all workspaces** leave the title-bar menus, and the workspace list leaves the empty-desktop menu;
- any floating window that was masked to another workspace becomes visible.

Re-enable it and your workspaces come back exactly as they were. A column created while the mod was off joins whichever workspace is active when you turn it back on.

## Related pages

- [[Taskbar]] — where the pager and its dots live.
- [[Context-Menus]] — the full right-click reference, including pager dots, windows, and the empty desktop.
- [[Keyboard-Shortcuts]] — view and rebind the workspace shortcuts.
