Browserland ships a set of default keyboard shortcuts for the actions you use most: moving between columns, switching workspaces, launching terminals, and toggling window modes. Every shortcut is rebindable, so you can make the desktop match your muscle memory.

## How to rebind a shortcut

All shortcuts are user-rebindable in **Control Panel → Keybindings**. Open the [[Control Panel|Getting-Started]] (right-click the + button or the desktop → **🎛 Control panel**, or press `Ctrl+Alt+p`), find the action you want, and record a new combo for it.

- To change a binding, select the action and press the key combination you want to assign.
- Keyboard shortcuts are part of the per-host settings (a tab per broker), so a host can carry its own bindings.

The in-app guide (the "?" chip at the bottom-right of the taskbar) also injects one **Keyboard shortcuts** entry per bindable action, showing its current combo or "Unbound" — handy for checking what is mapped right now.

> **Toggle help is unbound by default.** The "Toggle help" action has no default key. Assign your own combo under **Control Panel → Keybindings** if you want to open the in-app guide from the keyboard. (You can always open it by clicking the "?" chip at the bottom-right of the taskbar.)

## Fixed terminal keys

A few keys inside a terminal are fixed rather than rebindable: `Ctrl+Shift+C` copies the current selection, `Ctrl+Shift+V` pastes, and on an https (or localhost) page `Alt+V` checks the clipboard for an image and pastes it into the terminal as an uploaded file path — with no image on the clipboard the keypress reaches the app unchanged, so Claude Code keeps its own `Alt+V` hotkey. See [[Window Types|Window-Types]] for how image paste works.

**Copy and paste into a terminal.** Selecting text with the mouse copies it the moment you release the button — there is no copy key to press. `Ctrl+Shift+C` is the explicit chord for the same thing, and it exists because plain `Ctrl+C` is left alone so it can still interrupt whatever is running. To paste, use your browser's paste shortcut (`Ctrl+V`, or `Cmd+V` on macOS), `Ctrl+Shift+V`, or right-click. Right-click pastes directly on an https or `localhost` page, though a browser may ask you to confirm the first time; on a plain-http LAN origin (`http://192.168.1.10:4445`) the browser refuses to hand the page your clipboard at all, so right-click is left alone and opens the browser's own menu, with its **Paste** entry, instead. The keyboard paste works on both, because there the text arrives *with* the keypress rather than being read out of the clipboard — as does any other browser paste action, including paste-as-plain-text, since terminal input is plain text either way. A multi-line paste goes in as one block rather than as separate lines, so a pasted command is not submitted at its first newline.

**Copying from inside a full-screen program.** A program with its own copy key — tmux copy-mode, Neovim, `lazygit`, a pager — cannot reach your laptop's clipboard directly, so it asks the terminal to do it for it. Browserland can honour that request, but it is **off by default** and you turn it on **per host**, under **Control Panel → that host's tab → Clipboard (OSC 52)**. The reason it is off is that everything downstream of the terminal can send this request: not just the program you are using, but a `cat` of a hostile file, a dependency's build output, or an SSH session to a machine you don't own — and the clipboard it would write is on the computer you are sitting at, not on the remote host. Enabling it for your own broker is a small step; enabling it for a box you don't control is a real one, which is why each host is a separate switch. The switch is stored in **this browser only** and never travels to a broker, so a broker cannot turn it on for itself.

Once it is on, a copy still needs the page to be on https or `localhost`, the window focused, and the copy to have followed something you actually did in the front terminal — a copy request that arrives while you are in another tab is dropped. Every refusal says so, and so does every accepted copy, naming the terminal it came from. Copies are capped at 1 MiB and are refused whole rather than truncated. A few deliberate limits worth knowing: text that is not valid UTF-8, or that contains control characters, is refused rather than silently mangled; in Neovim `"+y` works but `"*y` does not, because the browser has one clipboard and no separate X11 primary selection to write; and a program can never *read* your clipboard this way — that request is refused outright and is not behind any setting. tmux emits the request out of the box; Neovim needs `set clipboard+=unnamedplus` with its OSC 52 provider, or an explicit `vim.g.clipboard` OSC 52 block.

**When a full-screen app grabs the mouse.** Apps like `lazygit`, `btop` and `mc` switch mouse reporting on, and from that moment the terminal forwards clicks, drags and the wheel to the *app* instead of the browser: drag-select stops highlighting anything, and the wheel scrolls the app's own pane. Hold `Shift` and drag to force a selection anyway — on macOS hold `Option` instead, because `Shift` is not the Mac gesture — and `Shift+scroll` scrolls the terminal rather than the app. Both are taken before the app ever sees them, so they work no matter what it has enabled. (`Shift+scroll` only has somewhere to go if there *is* scrollback: an app drawing on the alternate screen, which most full-screen ones do, has none, so there its own scroll keys are what you want.) Plain `Ctrl+C` keeps its usual meaning and goes to the app — that is the point, it interrupts — which is exactly why the copy chord is `Ctrl+Shift+C`; pasting is unaffected, because that is the browser's own shortcut rather than a key the app ever sees. Enable the **Mouse mode chip** mod (Control Panel → Mods) and a 🖱 appears in the window's title bar for exactly as long as mouse reporting is on, with the escape gesture in its tooltip.

<!-- help:ignore-start -->
<!-- The in-app Help guide already injects one live "Keyboard shortcuts" entry
     per bindable action showing the user's CURRENT combo (or "Unbound"); a
     static default table would duplicate and could contradict it. Excluded
     from in-app Help only — still rendered on the GitHub wiki. -->
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
<!-- help:ignore-end -->

## What the actions do

The table above is authoritative. The notes below group the actions so you know where each one applies.

### Columns (tiling mode)

Focus column left/right and move column left/right shift your focus and your columns along the tiling strip. They apply when you are in tiling mode. See [[Columns and Widths|Columns-and-Widths]] for width presets and how columns work, and [[Window Modes|Window-Modes]] for switching into tiling mode.

### Workspaces

Previous/next workspace step through your virtual desktops; the "Go to workspace 1–5" actions jump straight to one. See [[Workspaces]] for the pager and sending windows between desktops.

These seven come from the **Workspaces** mod (enabled by default), so they leave this list if you turn it off — and your bindings for them come back untouched when you turn it back on.

### Windows

New terminal launches the **local broker's** default profile. Close focused window and Minimize focused window act on the front window.

### App-wide

Toggle tiling mode switches between floating and tiling (see [[Window Modes|Window-Modes]]). Toggle fullscreen, Open control panel, and Toggle help round out the global actions — Toggle help is unbound until you assign it a combo.

## See also

- [[Getting Started|Getting-Started]] — first steps with the desktop
- [[Window Modes|Window-Modes]] — floating vs. tiling and the tiling strip
- [[Columns and Widths|Columns-and-Widths]] — tiling columns, widths, and focus
- [[Workspaces]] — virtual desktops and the pager
