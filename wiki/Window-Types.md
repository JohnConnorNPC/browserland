Browserland's desktop holds five kinds of windows, all opened from the launch (**+**) button on the taskbar: a **terminal**, a **sticky note**, a **CodeMirror text editor**, a **file manager**, and a **task manager**. This page explains what each one is for and how it behaves.

## Opening windows from the + menu

The **+** button at the left of the taskbar is how you create windows:

- **Left-click +** to launch the local broker's default terminal.
- **Right-click +** for the full profile / app menu. Alongside the terminal profiles (grouped per broker when you have more than one host), it lists the app windows:

| Menu item | Opens |
|---|---|
| `📝 Sticky note` | A sticky note |
| `📄 Text editor` | A CodeMirror text editor |
| `🗂 File manager` | A file manager |
| `🧰 Task manager` | A task manager |

You can also open a terminal with the **New terminal** shortcut (`Ctrl+Alt+Enter` — see [[Keyboard-Shortcuts]]). See [[Taskbar]] for the rest of the + menu, including "Open in folder…" and the configurable Start button label.

## Terminal

A terminal runs a real shell on its broker host. The shell keeps running even when no browser is attached, so you can close the window, come back later, and the screen heals from a snapshot.

Each terminal title bar carries a per-window **robot button** that sets that window's MCP (AI agent) access — off, read, or read-write. The robot icon briefly flashes when an agent reads the screen or types into the terminal. See [[MCP-and-AI-Agents]] for the full picture.

A new terminal from the **+** button (left-click) or `Ctrl+Alt+Enter` always runs on the **local broker**; to start one on a remote host, pick its profile from the right-click **+** menu. See [[Hosts-and-Multi-Browser]] for adding hosts.

## Sticky note

A sticky note is a small, always-visible scratch pad for quick text. Notes stay out of the taskbar.

If you close a sticky note that still has text in it, it is not lost: it appears under a **Closed notes** section at the bottom of the **+** (launch) menu. Click it there to reopen it. An empty note you close is simply discarded.

## CodeMirror text editor

The text editor is a CodeMirror-backed editor with syntax highlighting, used to view and edit files. It opens at the **active terminal's working directory and host**, so it follows wherever you currently are.

The editor's content is backed by a real file on the host. Closing the editor with unsaved changes prompts you to save first; the editor window itself is not kept around, but your file on disk is safe once written.

## File manager

The file manager is a dual-pane file browser for moving around the filesystem and opening files into the text editor. Like the editor, it opens at the **active terminal's working directory and host**, so it follows where you are working.

## Task manager

The task manager is a live monitor — not a saved document. It lists every running terminal / agent session across all hosts, and each entry expands to its child-process tree. From there you can **End** an individual process, or **destroy** the whole session — which kills its shell and closes the window.

Because it is a live monitor, the task manager is never saved: closing it just dismisses it.

## Closing, terminating, and deleting

How a window goes away depends on its type:

- **Close** is soft, but what it preserves varies. A terminal's shell keeps running so you can reattach later. A non-empty sticky note is retained and reopens from **Closed notes** in the **+** menu (an empty note is discarded). A text editor's content lives in a file on the host, so closing it (after the save prompt) leaves your file intact. A file manager, the task manager, and the Control Panel/help are ephemeral — closing them just dismisses them.
- **Terminate** (terminals only) hard-kills the shell process tree.
- **Delete** (on note, editor, and file-manager windows — not the task manager, Control Panel, or help — shown as *Delete note* / *Delete file*) permanently discards that window and its stored document.

These actions live on the title-bar right-click menu. For the full semantics — plus minimize, pin, recolor, rename, and arrange-all — see [[Floating-Window-Controls]].
