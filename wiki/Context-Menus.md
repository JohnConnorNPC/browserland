Almost every action in Browserland has a right-click home. This is the "where do I right-click for X" index: pick the surface you're pointing at, see what its menu offers, and jump to the page that explains each action in depth.

A few rules apply everywhere:

- Greyed-out items don't apply to the current window (for example, **Move column left** when the window is already in the leftmost column).
- A `✓` marks the current setting (the active width preset, the current MCP mode, names-vs-numbers, and so on).
- Press `Escape`, or click anywhere outside the menu, to dismiss it.

## At a glance

| Surface | How to open | Covered in depth on |
|---|---|---|
| Window title bar | Right-click a window's title bar | [[Arranging-Windows]], [[Columns-and-Widths]], [[Floating-Window-Controls]] |
| Tab strip | The `⊟` button on a tile's tab strip | [[Arranging-Windows]] |
| Taskbar item | Right-click a window's taskbar button | [[Taskbar]] |
| Pager dot | Right-click a workspace dot in the taskbar | [[Workspaces]] |
| Empty desktop / strip | Right-click the background (or empty taskbar) | [[Floating-Window-Controls]], [[Workspaces]] |
| Launch (+) button | Right-click the `+` button | [[Taskbar]], [[Window-Types]] |

## Window title bar

Right-click a window's title bar. The menu adapts to whether the window is **tiled** or **floating**, and to whether it's a terminal or an app window (note, editor, file manager, task manager, Control Panel, help).

**When the window is tiled:**

- **Column width** — set the column to `⅓`, `½`, `⅔`, or `max`. See [[Columns-and-Widths]].
- **Stack into left column** / **Stack into right column** — merge this window down into a neighbor column as a row.
- **Move to own column** — eject a window that shares a column into its own.
- **Move to new column** — spawn a fresh column to its right.
- **Tab into left column** / **Tab into right column** — tab this window into the neighbor column's live tile.
- **Tab this window** — seed or keep a tab strip in place for a lone window.
- **Untab tile (split to rows)** — break a tabbed tile back into stacked rows.
- **Untab cell (side by side)** — drop a nested split-group's tabs out as adjacent side-by-side cells.
- **Un-split row (split to rows)** — explode a split row into stacked rows.
- **Move column left** / **Move column right** — shift the whole column along the strip.
- **Send to &lt;workspace&gt;** / **Send to new workspace** — move the window to another virtual desktop. See [[Workspaces]].
- **Float this window** — detach it from the strip as a floating window. See [[Snapping-and-Pop-out]].

Tabbing, splitting, and stacking are all explained on [[Arranging-Windows]]; the column actions live on [[Columns-and-Widths]].

**When the window is floating:**

- **Tile this window** — drop it straight into the tiling strip (no drag needed). See [[Snapping-and-Pop-out]].
- **Lock to screen (pin)** / **Unlock (scroll with strip)** — pin the window so it doesn't scroll away with the tiling strip (this pins position, not always-on-top).
- **On all workspaces** / **Show on all workspaces** — keep a floating window visible on every workspace, or limit it to this one. See [[Workspaces]].

**On every window:**

- **MCP access** (terminals only) — set this window's agent access to **Off**, **Read**, or **Read-write**. See [[MCP-and-AI-Agents]].
- **Minimize** / **Restore** — hide the window to the taskbar, or bring it back.
- **Close** — soft close. A terminal's shell keeps running; a non-empty sticky note reopens from *Closed notes* in the **+** menu; a text editor keeps its file on the host (with a save prompt for unsaved changes); a file manager / task manager just closes.
- **Terminate** (terminals only) — hard-kill the shell process tree (asks to confirm).
- **Delete note** / **Delete file** (note and editor windows — not the file manager, task manager, Control Panel, or help) — permanently discard that window and its stored document (asks to confirm).

Close, terminate, delete, and pinning are detailed on [[Floating-Window-Controls]].

> **Color and rename** aren't on this right-click menu. Recolor a window from the **color button** in its title bar (palette, custom, and recents), and **double-click** an app window's title to rename it — both covered on [[Floating-Window-Controls]].

## Tab strip

When windows are stacked as tabs in one tile, the tile shows a tab strip. The strip's `⊟` button untabs it:

- On a **top-level tabbed tile**, `⊟` does **Split into rows** (the same as the title-bar's **Untab tile (split to rows)**).
- On a **nested split group**, `⊟` does **Drop tabs side by side** (the same as **Untab cell (side by side)**).

Click any tab to switch to that window. Tabs and untabbing are explained on [[Arranging-Windows]].

## Taskbar item

Each open window has a taskbar button. Left-click focuses and raises it (or restores it if minimized). Right-click for per-window actions:

- **Focus** — switch to the window's workspace, restore it if minimized, and raise it. This works even for a closed or parked window (it reopens it).
- **Restore** / **Minimize** — bring a minimized window back, or hide it.
- **Close** — soft close, same as the title-bar menu.
- **Terminate** (terminals only) — hard-kill the shell, even on a parked session (asks to confirm).

To send a window to another workspace, use its **title-bar** menu (above). The taskbar is covered on [[Taskbar]].

## Pager dot

Right-click a workspace dot at the bottom of the taskbar:

- **Rename…** — give the workspace a name.
- **Remove workspace** — delete it (disabled when only one workspace remains).
- **Show names** / **Show numbers** — choose whether the dots display workspace names or numbers.
- **New workspace** — append a fresh, empty workspace.

Workspaces and the pager are covered on [[Workspaces]].

## Empty desktop / strip

Right-click the desktop background (or an empty part of the taskbar). The menu depends on the window mode.

**In floating mode** — one-shot arrangements plus a size lock:

- **Cascade**
- **Tile Horizontally**
- **Tile Vertically**
- **Tile H + V**
- **Lock Size** / **Unlock Size** — snap every floating window to the default size and hide the resize handles, or restore free resizing.
- **Minimize All Windows**
- **Undo &lt;action&gt;** — appears after an arrange, to reverse the most recent one (single-level).

**In tiling mode** — a workspace switcher:

- A row per workspace, showing its name and column count (the `✓` marks the active one); click to jump to it.
- **New workspace** — append a fresh, empty workspace.

Both modes also offer **🎛 Control panel**. The arrange and lock-size actions are covered on [[Floating-Window-Controls]]; the workspace list on [[Workspaces]].

## Launch (+) button

Left-click the `+` to launch the default host's default terminal — the local broker unless you've picked another default in Control Panel → Hosts (see [[Hosts-and-Multi-Browser]]). Right-click it for the full launch menu:

- **Terminal profiles** — each launchable profile for the host (the default is marked `(default)`). With more than one host, profiles are grouped under per-host headers.
- **Open in folder…** — a directory picker (per host) that starts the host's default profile rooted at the folder you choose.
- **App windows** — **📝 Sticky note**, **📄 Text editor**, **🗂 File manager**, **🧰 Task manager**, **🎛 Control panel**, **❓ Help**, **Scratchpad**, and **⏺ Session recorder**. See [[Window-Types]].
- **Closed notes** — at the bottom, any non-empty sticky note you've closed; click one to reopen it.

The launch button and start-button label are covered on [[Taskbar]].
