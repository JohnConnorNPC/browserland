The clipboard history keeps a rolling list of the last 20 things you have copied or pasted, in both directions, so you can grab an earlier value back after a newer copy has overwritten it. It appears whenever the Clipboard mod is enabled — there is no separate toggle. Turn it on or off from Control Panel → Mods.

The mod ships **disabled by default**. Clipboards carry secrets — passwords, tokens, keys — so it captures **nothing** until you opt in, and the moment you disable it capturing stops again. Enable it from Control Panel → Mods.

## Opening the window

Open a Clipboard window from the desktop **+** (launch) menu → **📋 Clipboard**. You can open more than one; every clipboard window shows the same shared history. For more on the launch menu, see [[Taskbar]].

## What it captures

While enabled, it records each copy and paste as you work:

- Text **copied out** of a terminal — by selecting it (auto-copy) or with `Ctrl+Shift+C`.
- Text **pasted in** — with `Ctrl+V`, a right-click, or the context-menu **Paste**.

Each entry shows a direction arrow (**→** copied out, **←** pasted in), the time it was captured, and a one-line preview of the text. Repeating an identical copy or paste just refreshes the existing entry's time instead of adding a duplicate, and only the most recent 20 entries are kept.

## Re-copying and clearing

Click any entry to copy its full text back to the clipboard — handy when a newer copy has clobbered something you still need. **Clear history** empties the list immediately, in every open clipboard window at once.

## Nothing is saved

The history lives in memory only. It is never written to disk or browser storage, so reloading the page clears it and it never restores across sessions — the safest posture for text that may contain secrets.
