Workspaces are virtual desktops. Each workspace is its own desktop of windows, so you can keep one set of terminals, notes, and editors on workspace 1 and a completely different set on workspace 2. Only one workspace is shown at a time.

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

## Related pages

- [[Taskbar]] — where the pager and its dots live.
- [[Context-Menus]] — the full right-click reference, including pager dots, windows, and the empty desktop.
- [[Keyboard-Shortcuts]] — view and rebind the workspace shortcuts.
