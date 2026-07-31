The taskbar runs along the bottom of the Browserland desktop. It holds the launch button, a button for each open window (sticky notes are the exception — they stay off the taskbar unless you opt them in), the workspace pager, host status, a fullscreen button, and clock, AI status, clipboard, and help chips.

## Anatomy

From left to right, the taskbar contains:

| Element | What it does |
|---|---|
| Launch button (**+**) | By default, left-click launches a terminal on your default host — the local broker unless you pick another under Control Panel → Hosts (see [[Hosts-and-Multi-Browser]]) — and right-click opens the full profile / app menu — a Control Panel toggle can swap the two |
| Window buttons | One per open window (sticky notes only when opted in — see below) — click to focus, right-click for per-window actions |
| Pager dots | One dot per workspace — click to switch. From the Workspaces mod, enabled by default (see [[Workspaces]]) |
| Host status | One status chip for a single broker; with several, one aggregate badge that opens the broker menu. A Control Panel option decides whether it shows always, only when a broker needs attention, or never (see [[Hosts-and-Multi-Browser]]) |
| Fullscreen button (`⛶`) | Toggles the browser into fullscreen |
| Clock chip | Date & time readout, shown while the Clock mod is enabled |
| AI status chip | Worst-case health of the major AI providers — click to open the status window. Shown while the **AI status** mod is enabled; it ships **off by default** because enabling it lets the broker fetch each provider's public status page, which makes the broker's egress IP visible to those hosts. Turn it on from Control Panel → Mods |
| Clipboard chip (**📋**) | Opens (or focuses) the clipboard-history window. Shown while the **Clipboard** mod is enabled; it ships **off by default** because clipboards carry secrets, so it captures nothing until you opt in. Turn it on from Control Panel → Mods |
| Help chip (**?**) | Opens the in-app interface guide (see [[Getting-Started]]) |

## Window buttons

Each open window has a button on the taskbar — except sticky notes, which by default stay off the taskbar and are reached from the **+** menu instead. If you'd rather have them there, the Sticky mod's **show sticky-note windows on the taskbar** setting (Control Panel → Mods) gives every open note a button too, applied live to notes already open.

- **Click** a button to focus and raise that window. If the window is minimized, clicking it restores it; if it lives on another workspace, Browserland switches to that workspace first. Clicking the button of the window that is **already focused and on top minimizes it** — so the same button toggles a window in and out of view.
- **Right-click** a button for per-window actions:

| Action | Effect |
|---|---|
| Focus | Switch to the window's workspace (if needed) and raise it, restoring it if minimized |
| Minimize / Restore | Hide the window to the taskbar, or bring it back |
| Close | Soft-close the window (a terminal's shell keeps running; a non-empty sticky note reopens from the **+** menu; a text editor keeps its file on disk) |
| Terminate | Terminals only — hard-kill the shell process tree |

The taskbar menu does not have a send-to-workspace item — to move a window to another workspace, use its **title-bar** right-click menu (see [[Workspaces]]). For the difference between Close, Terminate, and Delete, see [[Floating-Window-Controls]].

### Label components

Each window button — and the matching window title bar — shows a label built from up to four components:

| Component | Example | Notes |
|---|---|---|
| **window id** | `#42` | The broker-assigned window number. On the taskbar it is dimmed and auto-hides on a very narrow button; in a window title bar it stays a leading badge |
| **host** | `myhost:` | The machine the session reports running on. The colon appears only when the host sits immediately before the title |
| **title** | `claude` | The program or agent title (or `session <id>` when it has none) |
| **pid** | `[12345]` | The process id, in brackets |

By default the label reads **`#id host: title [pid]`** with the pid hidden — for example `#42 myhost: claude`.

To change it, open **Control Panel → Windows → Taskbar / title labels**. Each component has a checkbox to show or hide it, and the rows can be reordered — drag a row, or use its ↑/↓ buttons — so the label follows the order you choose. At least one component always stays ticked (you cannot hide them all), and a ticked component that has no value for a given window (for example the host on an app window that has none) is simply left out — if that would leave the label empty, the title is shown instead, so the label is never blank. The hover tooltip always lists everything regardless of these settings. Like the other display toggles, this preference is stored per broker host and shared with your other browsers viewing that host.

### Items for other workspaces

By default, buttons for windows on other workspaces still appear (dimmed) so you can jump to them. To show only the active workspace's windows in the taskbar, turn on **Hide taskbar items from other workspaces** under Control Panel → Windows → Workspaces. This setting governs your browser.

## The launch button (+)

The launch button doubles as a Start button.

By default:

- **Left-click** launches a terminal using your **default host's** default profile. The default host is the local broker unless you pick another under Control Panel → Hosts (see [[Hosts-and-Multi-Browser]]).
- **Right-click** opens the full launch menu: the launchable terminal profiles plus the other window types (sticky note, text editor, file manager, task manager, scratchpad, session recorder, control panel, and help — with more entries, such as an AI-provider status monitor, when the matching mods are enabled; see [[Window-Types]] for the authoritative list). Every broker leads with a **live status row** (its state, and the old chip's click actions — log in, take over the lease, or hide/show its windows, which keeps the menu open) with its profiles below it, so you can launch on a remote host from here. With several hosts attached, the taskbar's aggregate broker badge opens this same menu (see [[Hosts-and-Multi-Browser]]).

### Swapping the click gestures

If you open the picker more often than you use the one-click default, turn on **Control Panel → Desktop → Start button → "Left-click opens the profile menu (right-click quick-launches)"**. With it enabled the two gestures swap: **left-click** opens the launch menu and **right-click** quick-launches your default host's default profile. The native browser context menu never appears either way. The toggle is off by default and, like the button label, applies to the browser you set it from.

### Open in folder…

The right-click menu also includes an **Open in folder…** item under each host's profiles. It opens a directory picker on **that host**, then starts that host's default profile rooted at the folder you choose. The picker browses the host you'll launch on (not your local machine), so the chosen path exists there. Cancel the picker to do nothing.

### Renaming the button

The button shows `+` by default. To change its label, set Control Panel → Desktop → Start button. Leave it blank to fall back to `+`. Only the visible label changes — the click gestures follow whichever mapping you have set (see [Swapping the click gestures](#swapping-the-click-gestures) above).

## Fullscreen

The fullscreen button (`⛶`) toggles the browser into fullscreen and back. You can also bind a key to it — the default is `Ctrl+Alt+f` for the **Toggle fullscreen** action. See [[Keyboard-Shortcuts]].

## Pager dots

The pager dots on the right of the taskbar (left of the host chips) are one-click workspace switchers, and their right-click menus rename or remove workspaces and toggle names vs. numbers. They come from the **Workspaces** mod (enabled by default), so the dots are absent if you turn it off. They are covered in full under [[Workspaces]].

To keep workspaces but reclaim the space the dots take, turn on **Hide the workspace pager from the taskbar** under Control Panel → Windows → Workspaces. Only the pager goes — the chip badges and dimming, the shortcuts, and **Send to workspace** all stay, and the workspace menu (right-click an empty part of the taskbar or the desktop) still switches, adds, renames, and removes workspaces.

## Broker status chip

The host-status area shows a chip for your broker (or one aggregate badge once you have several). By default it is always there, but it is only an indicator — every broker also has a live row in the start (+) menu carrying the same actions — so **Control Panel → Desktop → Broker status chip** lets you turn it down:

| Show | Effect |
|---|---|
| Always | The default, and the same as before this option existed |
| Only when a broker needs attention | The chip appears only while a broker is unreachable, wants a password, or is held by another browser — and disappears again once nothing is wrong |
| Never | The chip is never drawn |

A broker **you hid yourself** never counts as needing attention, so parking one you don't want right now keeps the chip quiet in attention mode. Nothing becomes unreachable when the chip is gone: right-click the **+** button and every broker is listed with its state and its log-in / take-over / un-hide action, and the same hide toggle is a checkbox on each host's row under Control Panel → Hosts. Attention mode is also deliberately slow to fire — a broker has to miss two polls (about four seconds) before it counts as down, so a single dropped poll never makes the chip flicker into view.

The setting follows the browser you are sitting at rather than any one host, and is shared with other browsers viewing the same broker. See [[Hosts-and-Multi-Browser]] for what each chip state means.
