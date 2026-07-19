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
is recorded as timestamped **markers only** — never the keystrokes themselves,
so a password typed during a recording is not stored. If the connection drops
and reattaches mid-recording, a gap marker is recorded (shown red on the
timeline) and the replay heals with the reattach redraw.

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
