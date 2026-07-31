Browserland's look comes from three independent mods: a **color scheme**, a **background pattern**, and the **terminal font**. All three are configured from the [[Control Panel|Getting-Started]]'s **Desktop** applet, and all three are browser-global — they follow the browser you are sitting at, not any one broker, so they stay the same across every host you connect to from here.

## Color scheme

The **Color scheme** setting (Control Panel → Desktop) picks the desktop's chrome colors — background, panel and accent tones. It ships enabled by default.

| Scheme | Look |
|---|---|
| Night (dark) | The default — a dark desktop |
| Day (light) | A light desktop |
| Redmond (teal) | A period-desktop look: teal desktop, silver window chrome |
| Midnight Blue | A dark blue desktop |
| Sunday Orange | A warm dark-orange desktop |

Picking a scheme applies immediately and repaints the background pattern (below) in the new colors too, since the pattern is drawn from the scheme's own tones.

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

The **Terminal font** setting changes the font every terminal window renders with. Unlike the two settings above, it ships **off by default** as a mod: enable **Terminal font** under **Control Panel → Mods** first (see [[Installing-Mods]]), which takes effect on the **next page load**. Once enabled, its select appears alongside Color scheme and Background pattern in Control Panel → Desktop.

The offered fonts are:

- Default (Consolas)
- Cascadia Code
- Fira Code
- JetBrains Mono
- Source Code Pro
- Courier New
- System monospace

Picking a font applies immediately to every open terminal window, and to every terminal you open afterward — no reload needed. A font you pick but don't have installed simply falls back to the default. If you later disable the mod, terminals return to the built-in default font.

## Related pages

- [[Getting-Started]] — opening the Control Panel and finding the Desktop applet.
- [[Installing-Mods]] — enabling a mod that ships off by default, like Terminal font.
- [[Taskbar]] — the clock, AI status, and other chips that also live in appearance-adjacent Control Panel settings.
