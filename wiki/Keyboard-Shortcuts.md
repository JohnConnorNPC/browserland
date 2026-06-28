Browserland ships a set of default keyboard shortcuts for the actions you use most: moving between columns, switching workspaces, launching terminals, and toggling window modes. Every shortcut is rebindable, so you can make the desktop match your muscle memory.

## How to rebind a shortcut

All shortcuts are user-rebindable in **Control Panel → Keyboard shortcuts**. Open the [[Control Panel|Getting-Started]] (right-click the + button or the desktop → **🎛 Control panel**, or press `Ctrl+Alt+p`), find the action you want, and record a new combo for it.

- To change a binding, select the action and press the key combination you want to assign.
- Keyboard shortcuts are part of the per-host settings (a tab per broker), so a host can carry its own bindings.

The in-app guide (the "?" chip at the bottom-right of the taskbar) also injects one **Keyboard shortcuts** entry per bindable action, showing its current combo or "Unbound" — handy for checking what is mapped right now.

> **Toggle help is unbound by default.** The "Toggle help" action has no default key. Assign your own combo under **Control Panel → Keyboard shortcuts** if you want to open the in-app guide from the keyboard. (You can always open it by clicking the "?" chip at the bottom-right of the taskbar.)

## Default bindings

The table below lists every bindable action and its **default** binding. Rebind any of them as described above.

| Action label | Default binding |
|---|---|
| Focus column left | `Ctrl+Alt+ArrowLeft` |
| Focus column right | `Ctrl+Alt+ArrowRight` |
| Move column left | `Ctrl+Alt+Shift+ArrowLeft` |
| Move column right | `Ctrl+Alt+Shift+ArrowRight` |
| Previous workspace | `Ctrl+Alt+ArrowUp` |
| Next workspace | `Ctrl+Alt+ArrowDown` |
| Go to workspace 1 | `Ctrl+Alt+1` |
| Go to workspace 2 | `Ctrl+Alt+2` |
| Go to workspace 3 | `Ctrl+Alt+3` |
| Go to workspace 4 | `Ctrl+Alt+4` |
| Go to workspace 5 | `Ctrl+Alt+5` |
| New terminal | `Ctrl+Alt+Enter` |
| Toggle tiling mode | `Ctrl+Alt+t` |
| Close focused window | `Ctrl+Alt+w` |
| Minimize focused window | `Ctrl+Alt+m` |
| Toggle fullscreen | `Ctrl+Alt+f` |
| Open control panel | `Ctrl+Alt+p` |
| Toggle help | *(unbound by default)* |

## What the actions do

The table above is authoritative. The notes below group the actions so you know where each one applies.

### Columns (tiling mode)

Focus column left/right and move column left/right shift your focus and your columns along the tiling strip. They apply when you are in tiling mode. See [[Columns and Widths|Columns-and-Widths]] for width presets and how columns work, and [[Window Modes|Window-Modes]] for switching into tiling mode.

### Workspaces

Previous/next workspace step through your virtual desktops; the "Go to workspace 1–5" actions jump straight to one. See [[Workspaces]] for the pager and sending windows between desktops.

### Windows

New terminal launches the **local broker's** default profile. Close focused window and Minimize focused window act on the front window.

### App-wide

Toggle tiling mode switches between floating and tiling (see [[Window Modes|Window-Modes]]). Toggle fullscreen, Open control panel, and Toggle help round out the global actions — Toggle help is unbound until you assign it a combo.

## See also

- [[Getting Started|Getting-Started]] — first steps with the desktop
- [[Window Modes|Window-Modes]] — floating vs. tiling and the tiling strip
- [[Columns and Widths|Columns-and-Widths]] — tiling columns, widths, and focus
- [[Workspaces]] — virtual desktops and the pager
