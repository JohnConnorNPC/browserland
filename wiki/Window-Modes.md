Browserland has two window modes that change how your windows are placed on the desktop: **floating** and **tiling**. This page explains the difference, how to switch, and how the tiling strip works.

## The two modes

| Mode | What it does |
|---|---|
| **Floating** | Free-moving, overlapping windows. You place each window anywhere, drag its title bar to move it, and drag its edges or corners to resize it. Windows can sit on top of one another. |
| **Tiling** | A niri-style horizontal strip of **non-overlapping columns**. Windows live side by side in columns; nothing overlaps, and the strip scrolls left and right when there are more columns than fit on screen. |

The window mode is a per-host setting (one tab per broker), so each host can be in its own mode.

## Switching modes

You can flip between floating and tiling two ways:

- **Control Panel → Window mode.** Open the [[Control Panel|Getting-Started]] (right-click the + button or the desktop → **🎛 Control panel**, or press `Ctrl+Alt+p`), then pick the window mode.
- **The "Toggle tiling mode" shortcut** — default `Ctrl+Alt+t`.

The shortcut is rebindable like every other keyboard action. See [[Keyboard-Shortcuts]] to change it.

## The tiling strip

In tiling mode, windows live in **columns** arranged along a horizontal strip. The strip can be wider than your screen, so only some columns are visible at once and the rest scroll into view.

### Scrolling the strip

There are three ways to move the strip left and right:

- Use the **workspace scrollbar**.
- **Drag a window near the strip edge** — dragging toward the left or right edge scrolls the strip in that direction.
- Use the **focus-column** and **move-column** shortcuts to bring off-screen columns into view as you focus or move them.

| Action | Default binding |
|---|---|
| Focus column left | `Ctrl+Alt+ArrowLeft` |
| Focus column right | `Ctrl+Alt+ArrowRight` |
| Move column left | `Ctrl+Alt+Shift+ArrowLeft` |
| Move column right | `Ctrl+Alt+Shift+ArrowRight` |

For column widths, moving and focusing columns, and ejecting a window into its own column, see [[Columns and Widths|Columns-and-Widths]].

## Moving windows between the two modes

You don't have to switch the whole desktop to move a single window between styles:

- **Snap a floating window into the strip**, or **pop a tiled window out to a float**, using the hold-to-snap and pop-out drag gestures — see [[Snapping and Pop-out|Snapping-and-Pop-out]].
- Inside tiling mode, organize windows into **tabs, splits, and rows** by dragging them onto each other — see [[Arranging Windows|Arranging-Windows]].

## Related pages

- [[Columns and Widths|Columns-and-Widths]] — width presets and moving/focusing columns in the tiling strip.
- [[Arranging Windows|Arranging-Windows]] — tabs, splits, stacks/rows, and the drag drop-zone cheat sheet.
- [[Snapping and Pop-out|Snapping-and-Pop-out]] — the gestures that move one window between floating and tiling.
- [[Keyboard-Shortcuts]] — the full shortcut table and how to rebind keys.
