Record any terminal session and replay it later, exactly as it looked.

## Recording

Every terminal title bar carries a red ⏺ button. Click it to start recording
that terminal; the button pulses and an elapsed timer appears beside it. Click
again to stop — the recording is saved to the broker automatically (it also
saves if you close the window mid-recording). Recordings live on the broker's
disk beside its state store (`webterm_recordings/`, override with
`recordings_dir` in the broker config) and are never expired — only deleting
them from the library removes them.

What is captured: the terminal's raw output stream (byte-faithful, so colors
and TUI apps replay exactly), resizes, and the initial size/font. Typed input
is recorded as timestamped **markers only** — the keystrokes themselves are
never stored. If the connection drops and reattaches mid-recording, a gap
marker is recorded (shown red on the timeline) and the replay heals with the
reattach redraw.

**A recording sees whatever the screen saw.** Not storing keystrokes protects
input the terminal never echoes — a password at a `sudo` or `ssh` prompt does
not appear. It does *not* protect anything the terminal prints: a secret typed
or pasted onto a visible command line is echoed as output, and output is
captured byte for byte. The same goes for anything a command prints — API keys
in a `env`-style dump, tokens, connection strings. Recording also starts by
capturing the screen that was **already there**, so content that scrolled past
before you pressed ⏺ can still be in the file. Treat a `.blrec` like a screen
recording: check what is in it before sharing it.

To check a saved recording for this broker's own token, run
`python -m webterm.broker --scan-recordings`. It base64-decodes the output
stream first -- a plain `grep` over a `.blrec` finds nothing even when the
token is in there, and reports a false all-clear. It only finds a secret that
appears as contiguous bytes, so a clean result is evidence, not proof.

A recording is held in memory until you stop it — reloading the page discards
an in-progress recording (the browser warns first). Recordings auto-stop at
50 MB of captured output.

## Playback

Open **Session recorder** from the right-click (+) menu to list recordings —
play, download (`.blrec`, newline-delimited JSON), or delete (click ✕ twice)
each one. **Play** opens a player window fixed at the recording's original
columns×rows — the window sizes itself to the recording, and follows any
resizes that happened during it.

Transport controls: play/pause, playback speed 0.25×–8×, and **◀◀ reverse** —
a true backwards animation, not just a jump. The scrubber seeks anywhere;
seeking and reverse render from keyframes the player indexes when it loads a
recording ("indexing…"). Note markers are gold on the timeline; connection
gaps are red.

## Notes

While playing (or paused), **✎+** adds a note at the current timestamp. Notes
appear as gold timeline markers — click one to jump there — and in the list
under the transport bar, where they can be edited (✎) or deleted (✕). Notes
are stored with the recording on the broker and survive reloads; concurrent
edits from two windows are revision-checked so nothing is silently lost.
