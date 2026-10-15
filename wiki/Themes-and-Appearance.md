Browserland's look comes from three independent mods: a **color scheme**, a **background pattern**, and the **terminal font**. All three are configured from the [[Control Panel|Getting-Started]]'s **Desktop** applet, and all three apply to every browser viewing this broker: they are saved with the broker that serves the page (its /state), not per host, so they stay the same across every host you connect to from here.

## Color scheme

The **Color scheme** setting (Control Panel → Desktop) picks the desktop's chrome colors — background, panel and accent tones. Terminal contents keep their own colors; the scheme changes only the desktop and window chrome. It ships enabled by default.

| Scheme | Look |
|---|---|
| Night (dark) | The default — a dark desktop |
| Day (light) | A light desktop |
| Redmond (teal) | A period-desktop look: teal desktop, silver window chrome |
| Midnight Blue | A dark blue desktop |
| Sunday Orange | A warm dark-orange desktop |

Picking a scheme applies immediately and repaints the background pattern (below) in the new colors too, since the pattern is drawn from the scheme's own tones.

Night (dark) is the desktop's own built-in look rather than something the mod paints over it — so picking Night, and disabling the Color scheme mod altogether, both land on exactly the same desktop. Either way the change is immediate; nothing waits for a reload.

## Background pattern

The **Background pattern** setting (Control Panel → Desktop) paints a pattern on the desktop, behind the tiling strip and any floating windows. It ships enabled by default, set to **None**.

| Pattern | Look |
|---|---|
| None | The default — a plain desktop, no pattern |
| Weave | A crosshatched weave |
| Dither | A checkered dither |
| Dots | A grid of dots |
| Hatch | Diagonal hatching |
| Tiles | A grid of tile lines |

The pattern is drawn using the current color scheme's tones, so switching schemes recolors the pattern automatically — you never need to re-pick it after changing the scheme.

## Terminal font

The **Terminal font** setting changes the font every terminal window renders with. Unlike the two settings above, it ships **off by default** as a mod: enable **Terminal font** under **Control Panel → Mods** first (see [[Installing-Mods]] for the general toggle). It comes up at once, with no reload, and its select appears alongside Color scheme and Background pattern in Control Panel → Desktop.

The offered fonts are:

- Default (Consolas)
- Cascadia Code
- Fira Code
- JetBrains Mono
- Source Code Pro
- Courier New
- System monospace

Picking a font applies immediately to every open terminal window, and to every terminal you open afterward — no reload needed. (The letters change at once; the grid of rows and columns is re-measured a moment afterwards, once the far end confirms the new size.) A font you pick but don't have installed simply falls back to the default. If you later disable the mod, terminals return to the built-in default font — or to another installed font mod's font, if one is also styling them.

## On a phone

On a screen 520 px wide or less (a phone, or a very narrow window) the clock chip is hidden and title bars drop the window-id badge, the color button and an idle ⏺ record button so titles stay readable; the MCP button, minimize, × and a running record button stay. On touch screens up to 900 px wide the title-bar buttons are larger.

## Related pages

- [[Getting-Started]] — opening the Control Panel and finding the Desktop applet.
- [[Installing-Mods]] — enabling a mod that ships off by default, like Terminal font.
- [[Taskbar]] — the clock, AI status, and other chips that also live in appearance-adjacent Control Panel settings.
