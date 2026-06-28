In tiling mode you build your layout by **dragging windows onto each other**. Where you drop decides what happens: stack windows as tabs, split a tile side-by-side, stack panes as rows, or spin off a brand-new column. This page covers all three combine gestures plus the drop-zone overlay that previews every landing spot.

These gestures apply to the tiling strip. For the float/tile hold gestures (snapping a floating window in, popping a tiled one out), see [[Snapping-and-Pop-out]]. For sizing and moving whole columns, see [[Columns-and-Widths]]. Most of these actions also have a right-click menu equivalent — see [[Context-Menus]].

## Tabs: stack windows in one tile

Tabbed windows share a single tile and show a **tab strip**; only one is visible at a time.

- **Alt-drag** one window onto another to stack them as tabs sharing one tile.
- Click a tab in the tile's tab strip to switch between the tabbed windows.

The title-bar menu offers the same thing without dragging:

| Menu item | What it does |
|---|---|
| `Tab into left column` | Tab this window into the neighbor column's live tile (to the left) |
| `Tab into right column` | Tab this window into the neighbor column's live tile (to the right) |
| `Tab this window` | Seed or keep a tab strip in place |

### Untabbing

To pull a window back out of a tab strip, use the title-bar / tab context menu:

| Menu item | What it does |
|---|---|
| `Untab tile (split to rows)` | Breaks the tab strip into stacked rows |
| `Untab cell (side by side)` | Drops a nested split-group's tabs out as adjacent side-by-side cells |

## Splits: side-by-side panes

A split places two windows next to each other inside one tile.

- Drag a window onto the **LEFT or RIGHT interior** of another window to split that tile horizontally, placing the two side-by-side.
- Drag the **gutter** between the panes to resize them.

### Un-splitting

- Use the window context menu to merge a split back together, or just drag a pane somewhere else.
- `Un-split row (split to rows)` instead explodes a split row into stacked rows.

## Stacks: rows in a column

- Drag a window onto another window's **TOP or BOTTOM edge** to stack them as rows within the same column.
- Drag the **horizontal gutter** to resize the rows.

## The drop-zone cheat sheet

While you drag, a highlighted overlay previews exactly where the window will land. Aim for the zone that matches the layout you want:

| Drop on… | Result |
|---|---|
| A column's far **left / right edge** | A **NEW column** |
| A window's **top band** | **TAB** into that tile |
| A window's interior **left / right quadrant** | A **SPLIT** (side by side) |
| A window's interior **top / bottom quadrant** | A **SPLIT** into rows |
| The **bottom band** of the desktop | **FLOAT** the window |

Watch the overlay before you release: if the highlight doesn't show the layout you want, move the cursor to a different zone and the preview updates. Dropping on a column edge is the drag equivalent of the menu's "Move to new column" — see [[Columns-and-Widths]] for column moves and width presets.

## Related pages

- [[Snapping-and-Pop-out]] — hold-to-snap a floating window into the strip and pop a tiled one out to a float
- [[Columns-and-Widths]] — width presets, moving and focusing columns
- [[Context-Menus]] — the full right-click reference for tab / untab / split actions
- [[Window-Modes]] — floating vs. tiling and the tiling strip
