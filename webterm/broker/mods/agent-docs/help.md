Agent docs is the tabbed editor for a folder's **`AGENTS.md`** and **`CLAUDE.md`** — the instruction files an agent reads before working in that directory. It opens from the **📋** button on every terminal's title bar, pointed at that terminal's working directory and host, so you can read and edit a project's agent instructions without leaving the desktop.

The feature ships as its own mod that **requires the Text editor mod**: it reuses the editor's window, tabs, and save machinery. If you disable the Text editor mod, Agent docs is switched off with it (the Mods pane shows it as `needs: editor`) and the 📋 buttons disappear; re-enable the editor and they return. Turn Agent docs itself on or off from Control Panel → Mods.

## Opening the window

Click the **📋** button on a terminal's title bar to open (or focus) the Agent-docs window for that terminal's working directory. Re-clicking reuses the same window rather than opening a second one, and a freshly opened window docks as a tab beside the terminal it came from. Local and remote terminals that share a path each get their own window, so a remote shell edits the remote host's docs.

## The tabs

The window carries three tabs:

- **`AGENTS.md`** — the folder's agent instructions. If the file does not exist yet, an empty buffer opens and saving creates it.
- **`CLAUDE.md`** — its companion. Saving `AGENTS.md` keeps `CLAUDE.md` referencing it via an **`@AGENTS.md`** include, so the two stay linked.
- **⚙ Sections** — a synthetic tab (no file of its own) for managing a reusable **Sections library**: add, rename, reorder, and reset the section snippets you drop into a doc. The library is synced across windows.

Each document tab is a full editor — line numbers, wrapping, find, and dirty-save all work as in the Text editor. Closing the window with unsaved changes prompts you to save first.

## The template checklist

A new or sparse `AGENTS.md` offers a **template checklist**: tick the standard sections you want (workflow rules, house style, and the like) and they are inserted into the document, so a project's instructions start from a consistent skeleton instead of a blank page.
