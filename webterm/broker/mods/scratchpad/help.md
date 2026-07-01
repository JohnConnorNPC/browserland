The Scratchpad is a notes window whose text is stored on the broker, not in your browser. Because the notes live server-side, they are shared across every browser you open on the same broker and they survive a page reload or a cache clear. It is a single window: opening it again from the launch menu just focuses the one that is already open. Your notes are organized as tabs, each a small code editor, and every save is kept in a revision history you can browse and restore.

## Opening the window

Open the Scratchpad from the desktop **+** (launch) menu → **Scratchpad**. There is only ever one Scratchpad window; launching it again focuses the existing one rather than stacking another. For more on the launch menu, see [[Taskbar]].

The Scratchpad shares the text editor's code-editing engine, so it is only available when the **Text editor** mod is enabled — disabling the editor also hides the Scratchpad. Both are on by default.

## Tabs

Each note is a tab across the top of the window:

- **New tab** — click the **+** in the toolbar. A fresh **Notes** tab opens (up to 20 tabs).
- **Switch** — click a tab.
- **Rename** — double-click a tab's name, type the new name, and press **Enter** (or **Escape** to cancel).
- **Close** — click the **×** on a tab. The last remaining tab cannot be closed, so there is always at least one note.

Which tab is active is remembered per browser, so it is not counted as a content change and does not add a revision.

## Saving

Typing autosaves after a short pause — there is no Save button, though **Ctrl+S** forces an immediate save. Saves are content-only: switching tabs or moving the window never creates a revision. Note text is never written to browser storage, only to the broker.

Only the **active** browser can save. If you open the Scratchpad in a second browser while another is active, a banner appears and the notes are read-only there — you can still read them, but edits will not be saved until that browser becomes the active one. Click **Take over** in the banner (after making the browser active) to save your local changes.

## History

Click **History** in the toolbar to browse past revisions. Each save keeps the previous version in a bounded, newest-first ring (the oldest revisions eventually scroll off). Select a revision to preview its tabs and text, then click **Restore** to bring it back — restoring writes the old content as a brand-new save, so nothing is lost and you can restore again if you change your mind.
