Floating windows are free-moving, overlapping windows you can place anywhere on the desktop. This page covers how to move, resize, minimize, close, pin, recolor, and arrange them. For the snap-to-tile and pop-out gestures, see [[Snapping-and-Pop-out]].

## Move and resize

- **Move** — Drag a floating window's title bar.
- **Resize** — Drag its edges or corners.

Most of the remaining controls live on the **title-bar right-click menu** — right-click a window's title bar to open it. See [[Context-Menus]] for the full right-click reference across the desktop.

## Minimize, close, terminate, and delete

A floating window's title-bar menu lets you get a window out of the way or close it. It's important to know that **closing is soft** — what actually happens depends on the window type.

| Action | What it does |
|---|---|
| **Minimize** | Hides the window to the taskbar. |
| **Restore** | Brings a minimized window back (same menu item, when the window is minimized). |
| **Close** | Soft close. A **terminal**'s shell keeps running so you can reattach later. A **non-empty sticky note** is retained and reopens from *Closed notes* in the **+** menu (an empty note is discarded). A **text editor**'s content is a file on the host, so closing it (after a save prompt for unsaved changes) leaves your file intact. A **file manager**, the **task manager**, and the Control Panel/help are ephemeral — Close just dismisses them. |
| **Terminate** | Terminals only. Hard-kills the shell process tree. You're asked to confirm first. |
| **Delete note** / **Delete file** | App windows other than the task manager, Control Panel, and help (notes, the text editor, the file manager). Permanently discards that window and its stored document. You're asked to confirm first. |

Because a closed terminal keeps its shell alive, **Terminate** is the only way to actually kill the process tree. For a sticky note or an editor file, **Close** preserves the content (a note in *Closed notes*, a file on disk), while **Delete** is the one path that throws it away.

You can also minimize or close the focused window from the keyboard:

| Action | Default binding |
|---|---|
| Minimize focused window | `Ctrl+Alt+m` |
| Close focused window | `Ctrl+Alt+w` |

These are rebindable — see [[Keyboard-Shortcuts]].

### Reopening a closed note

If you close a sticky note that has content, it isn't lost: a non-empty closed note is listed under a **"Closed notes"** section at the bottom of the **+** (launch) menu. Click it there to reopen it. For more on the launch menu, see [[Taskbar]].

## Pin a window (lock to screen)

By default a floating window scrolls away with the tiling strip. To keep it put, use the title-bar right-click menu:

- **Lock to screen (pin)** — The window stays in place and does **not** scroll away with the strip. This pins its *position*, not always-on-top.
- **Unlock (scroll with strip)** — Reverses it, so the window scrolls with the strip again.

## Color and rename

- **Recolor** — Use the **color button** in the window's title bar. You can pick from the palette, choose a custom color, or reuse a recent one.
- **Rename** — Double-click an **app window's** title to rename it.

## Arrange all floating windows

Right-click the **empty desktop** in floating mode for one-shot arrangements that act on all floating windows:

| Item | Effect |
|---|---|
| **Cascade** | Stacks the windows in an overlapping cascade. |
| **Tile Horizontally** | Gives each window a full-width row, stacked top to bottom. |
| **Tile Vertically** | Gives each window a full-height column, placed side by side. |
| **Tile H + V** | Tiles the windows in a grid. |
| **Minimize All Windows** | Minimizes every floating window to the taskbar. |

After you run one, an **"Undo &lt;action&gt;"** item appears so you can reverse the most recent arrange. Undo is **single-level** — only the last arrangement can be undone.

## Lock Size (global)

The empty-desktop right-click menu also has a global **Lock Size** toggle:

- **Lock Size** — Snaps every floating window to the default size and hides the resize handles, so windows can't be resized.
- **Unlock Size** — Restores free resizing.

## Related

- [[Snapping-and-Pop-out]] — drag a floating window into the tiling grid, or pop a tiled window back out to a float.
- [[Window-Types]] — terminals, notes, the editor, the file manager, and the task manager.
- [[Context-Menus]] — the full right-click reference.
