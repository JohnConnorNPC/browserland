Browserland has two "hold-to-snap" gestures that move a window between the [[floating and tiling layouts|Window-Modes]] just by dragging and pausing: you can **snap** a floating window into the tiling grid, or **pop** a tiled window back out to a float. Each gesture also has a one-click shortcut on the title-bar [[right-click menu|Context-Menus]].

Both gestures share a single timer — the **drag hold delay** (the *dwell*). You start a drag, hold the window still long enough, and the window arms the gesture.

## Snap a floating window into the grid

Use this to drop a free-floating window into the tiling strip.

1. Start dragging the window's title bar.
2. **Hold it still** for the dwell (default ~3s) to enter snap mode.
3. The drop zones light up — the highlighted overlay previews where the window will land.
4. **Release** to tile the window where the overlay shows.

To cancel without tiling, drop the window on the top-left **"return to floating"** zone, or press `Escape`.

For the full map of edges, bands, and quadrants you can drop onto, see the [[drop-zone cheat sheet|Arranging-Windows]].

### Skip the drag

If you just want the window tiled and don't need to choose a spot, **right-click the title bar** and choose **"Tile this window"** to drop it straight into the strip.

## Pop a tiled window out to a float

Use this to detach a window from the tiling strip back into a free-floating window.

1. Start dragging the tiled window.
2. **Hold it still** for the dwell to arm **"release to float"** mode — a dashed outline appears and the bottom band lights up.
3. **Release** to detach the window as a floating window.

To cancel, **move the window again** before releasing — that keeps it tiling. Pressing `Escape` aborts the drag entirely.

### Skip the gesture

To detach a window directly without the hold, **right-click the title bar** and choose **"Float this window"**.

## Hold delay (configurable)

The hold time for **both** gestures is set per host in **Control Panel → Drag hold delay**, measured in milliseconds.

| Setting | Behavior |
|---|---|
| `0` | **Disables both** the snap and the pop-out gestures entirely |
| `250`–`20000` | Hold time before a gesture arms (default `3000`) |

Because this is a per-host setting, each broker you connect to keeps its own delay.

> Tip: Set the delay to `0` if you find windows snapping or floating by accident, and use the **"Tile this window"** / **"Float this window"** title-bar menu items instead.

## Related pages

- [[Arranging-Windows]] — the drop-zone cheat sheet for where a snapped window lands
- [[Window-Modes]] — floating vs. tiling and the tiling strip
- [[Context-Menus]] — the title-bar right-click actions, including float/tile
