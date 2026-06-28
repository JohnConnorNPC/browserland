In tiling mode, every window lives in a **column** on the horizontal strip. This page covers how to resize those columns with width presets, move and focus columns from the keyboard, and split a shared column out into its own.

These controls only apply in **tiling mode**. For an overview of floating vs. tiling and the strip itself, see [[Window Modes|Window-Modes]]. For tabs, splits, and the drag-to-merge drop zones, see [[Arranging Windows|Arranging-Windows]].

## Column width presets

A tiled column can be sized to one of four widths from the window context menu (right-click the title bar) or the width controls. The current width is marked with a check.

| Menu label | Width |
|---|---|
| `⅓` | One third of the strip |
| `½` | Half of the strip |
| `⅔` | Two thirds of the strip |
| `max` | Maximum width |

To set a preset, right-click the window's title bar and pick a width under the **Column width** heading.

### Fine-tune by dragging

For a width between the presets, drag a column's **side gutter** — the seam between two columns — left or right. This resizes the column to a custom width.

## Move & focus columns

You can shuffle columns and jump focus between them without touching the mouse. These are the defaults — all keys are rebindable in [[Control Panel → Keyboard shortcuts|Keyboard-Shortcuts]].

| Action | Default binding |
|---|---|
| Focus column left | `Ctrl+Alt+ArrowLeft` |
| Focus column right | `Ctrl+Alt+ArrowRight` |
| Move column left | `Ctrl+Alt+Shift+ArrowLeft` |
| Move column right | `Ctrl+Alt+Shift+ArrowRight` |

- **Focus** jumps your selection to the window in the neighboring column without moving or rearranging anything.
- **Move** picks up the focused column and swaps it with its neighbor, so you can reorder the strip.

## Eject a window into its own column

When two or more windows share a column (stacked as rows), the window context menu can pull one out:

| Menu item | What it does |
|---|---|
| **Move to own column** | Ejects a window that shares a column into its own column. Available only when the window is sharing. |
| **Move to new column** | Spawns a fresh column immediately to its right and drops the window there — the click equivalent of dragging a window to a column edge. |

`Move to new column` is the menu shortcut for the same result you get by dragging a window onto a column's far left or right edge (the **new column** drop zone). See the drop-zone cheat sheet in [[Arranging Windows|Arranging-Windows]] for the drag gestures.

## Related

- [[Window Modes|Window-Modes]] — switching to tiling mode and scrolling the strip
- [[Arranging Windows|Arranging-Windows]] — tabs, splits, stacking rows, and drop zones
- [[Keyboard Shortcuts|Keyboard-Shortcuts]] — rebind the focus/move-column keys
