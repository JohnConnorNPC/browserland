Browserland's desktop holds several kinds of windows, all opened from the launch (**+**) button on the taskbar: a **terminal**, a **sticky note**, a **CodeMirror text editor**, a **file manager**, a **task manager**, and the **Control Panel**. (The in-app **help** guide opens the same way — see [[Getting-Started]].) This page explains what each one is for and how it behaves.

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
| `🎛 Control panel` | The Control Panel (settings) |
| `❓ Help` | The in-app help guide |

You can also open a terminal with the **New terminal** shortcut (`Ctrl+Alt+Enter` — see [[Keyboard-Shortcuts]]). See [[Taskbar]] for the rest of the + menu, including "Open in folder…" and the configurable Start button label.

## Terminal

A terminal runs a real shell on its broker host. The shell keeps running even when no browser is attached, so you can close the window, come back later, and the screen heals from a snapshot.

Each terminal title bar carries a per-window **robot button** that sets that window's MCP (AI agent) access — off, read, or read-write. The robot icon briefly flashes when an agent reads the screen or types into the terminal. See [[MCP-and-AI-Agents]] for the full picture.

A new terminal from the **+** button (left-click) or `Ctrl+Alt+Enter` always runs on the **local broker**; to start one on a remote host, pick its profile from the right-click **+** menu. See [[Hosts-and-Multi-Browser]] for adding hosts.

## Control Panel

The Control Panel is where you configure the desktop — appearance, window mode, drag-hold delay, hosts, MCP access, keyboard shortcuts, and more, with a tab per connected broker. It opens as a **moveable floating window**: drag its title bar to move it, drag its edges to resize, minimize it to the taskbar, or tile it like any other window. Open it from the **+** menu's **🎛 Control panel** item, the desktop / taskbar right-click menu, or the **Open control panel** shortcut (`Ctrl+Alt+p`).

Like the file manager and the task manager, the Control Panel is **ephemeral**: it edits settings that persist on their own (per-browser, or per-host via the broker), so the window itself has nothing to save — closing it just dismisses it, and it has no *Delete*. See [[Getting-Started]] for what the settings cover.

## Closing, terminating, and deleting

How a window goes away depends on its type:

- **Close** is soft, but what it preserves varies. A terminal's shell keeps running so you can reattach later. A non-empty sticky note is retained and reopens from **Closed notes** in the **+** menu (an empty note is discarded). A text editor's content lives in a file on the host, so closing it (after the save prompt) leaves your file intact. A file manager, the task manager, and the Control Panel/help are ephemeral — closing them just dismisses them.
- **Terminate** (terminals only) hard-kills the shell process tree.
- **Delete** (on note, editor, and file-manager windows — not the task manager, Control Panel, or help — shown as *Delete note* / *Delete file*) permanently discards that window and its stored document.

These actions live on the title-bar right-click menu. For the full semantics — plus minimize, pin, recolor, rename, and arrange-all — see [[Floating-Window-Controls]].
