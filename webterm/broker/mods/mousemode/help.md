The Mouse mode chip puts a **🖱** on a terminal's title bar for exactly as long as that terminal has mouse reporting switched on, and names the way out in its tooltip. It appears whenever the Mouse mode chip mod is enabled — there is no separate toggle. Turn it on or off from Control Panel → Mods.

The mod ships **enabled**, because it costs nothing to leave on: the chip is invisible in an ordinary shell and only shows up when a full-screen app takes the mouse.

## Why the chip matters

Full-screen apps — `lazygit`, `btop`, `mc`, `htop`, most agent CLIs — ask the terminal to report mouse activity so they can implement their own clicking and scrolling. From that moment the terminal hands clicks, drags and the wheel to the app instead of the browser, and **dragging across the screen no longer selects anything**. Nothing normally tells you that happened; the selection simply stops working, usually minutes after the app printed its splash screen.

The chip is the missing signal. It is on precisely while reporting is on and gone the moment it is switched off — when you quit the app, or when it drops back to a plain prompt.

It reports the terminal's state, not the app's intent, and says so: **"Mouse reporting is on"**. Those are not the same claim. A TUI that crashed or was killed, or one that simply forgets to clean up — and even a `cat` of a file that happens to contain the escape sequence — leaves reporting on with nothing listening. The chip stays up, correctly, because your dragging really is being swallowed; press `Enter` at the shell prompt or run `reset` to clear it.

## The tooltip

Hover the chip and it tells you how much of the mouse was asked for (clicks, clicks and drags, or every mouse move) and how to work around it:

- **`Shift`-drag** selects text anyway — the terminal takes that gesture before the app sees it. On macOS hold **`Option`** instead; `Shift` is not the Mac gesture.
- **`Shift+scroll`** scrolls the terminal instead of the app. An app drawing on the alternate screen has no scrollback of its own to reach, so there this does nothing and the app's own scroll keys are what you want.
- **`Ctrl+Shift+C`** copies the selection, since plain `Ctrl+C` belongs to the app.

See [[Keyboard Shortcuts|Keyboard-Shortcuts]] for the full copy-and-paste story, including what pastes on a plain-http origin.

## What it does not do

The chip has no click action, no menu and no settings — it is a status light. It never changes what the app receives: enabling or disabling this mod cannot affect mouse reporting, only whether you are told about it.
