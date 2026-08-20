The Terminal font setting picks the font every terminal renders with, in place of the built-in monospace default.

The mod ships **disabled by default** — turn it on first in **Control Panel → Mods**, which then adds the **Terminal font** setting to **Control Panel → Desktop**. The choice is browser-global, so it applies to every terminal on every host you drive from this browser.

## The choices

**Default (Consolas)** (the fallback while unset), **Cascadia Code**, **Fira Code**, **JetBrains Mono**, **Source Code Pro**, **Courier New**, and **System monospace** — the browser's own generic monospace font. Picking a family that isn't actually installed falls back to whatever the browser substitutes for it; the mod doesn't install fonts, only selects among them.

## Applies live, per terminal

A change restyles every terminal already open, and every new one, without a reload. The new glyphs appear at once; the terminal's row/column grid is re-measured a moment later, once the far end confirms the new size.

Turning the mod off hands each terminal back: to another installed font mod if one is also styling it, otherwise to the built-in default. Choosing **Default (Consolas)** does the same thing without disabling the mod — it means "no font of mine", not "force Consolas over whatever else is here".
