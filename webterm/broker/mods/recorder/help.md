Record any terminal session and replay it later, exactly as it looked.

## Recording

Every terminal title bar carries a red ⏺ button. Click it to start recording
that terminal; the button pulses and an elapsed timer appears beside it. Click
again to stop — the recording is saved to the broker automatically (it also
saves if you close the window mid-recording). Recordings live on the broker's
disk beside its state store (`webterm_recordings/`, override with
`recordings_dir` in the broker config) and are never expired — only deleting
them from the library removes them.

## Auto-record every session

**Control Panel → Session recorder → auto-record every session** (off by
default) starts recording every terminal as it opens, without anyone pressing
⏺. Like the other browser-global settings it is shared across every browser
viewing this broker.

- Switching it **on** also starts recording the terminals that are already
  open.
- Switching it **off** stops — and saves — the recordings it started. A
  recording you started by hand with ⏺ keeps going; the setting only owns the
  ones it opened itself.
- ⏺ still works while it is on: pressing it stops that terminal's recording
  and it stays stopped. Pressing it again starts a new one.
- Nothing is captured while no browser has the terminal open. This records
  what a browser is *showing*, so a session running with the desktop closed is
  not recorded.

With auto-record on, a page reload does **not** warn before discarding the
segment in progress — a prompt on every single reload is worse than losing the
tail of a recording that will be re-armed on the next load. A recording you
started by hand still warns, and so does a reload while a recording is still
uploading.

Recordings are never swept, so leaving this on accumulates files on the
broker's disk indefinitely. There is no size or age limit yet — delete what you
do not need from the library.

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
recording: check what is in it before sharing it. Turning on auto-record makes
that guarantee more load-bearing, not less: everything every session prints is
captured, including the sessions you would not have thought to record.

**Clipboard copies are in the recording too.** When a program copies by asking
the terminal to set your clipboard (the per-host **Clipboard (OSC 52)** setting
— see the guide's Keyboard shortcuts page), capture happens *upstream* of the
part that decodes it, so what lands in the file is the raw escape sequence with
the copied text base64-encoded inside it. That is true whether or not you
enabled the setting: the recorder captures the request, not the result. It is
the same hazard as the paragraph above, minus the part where you can spot it by
eye — so scan rather than skim.

To check a saved recording for this broker's own token, run
`python -m webterm.broker --scan-recordings`. It base64-decodes the output
stream first -- a plain `grep` (or `zgrep`) over a recording finds nothing even
when the token is in there, and reports a false all-clear. It scans compressed
and uncompressed recordings alike. It only finds a secret that appears as
contiguous bytes, so a clean result is evidence, not proof.

Recordings are stored **gzipped** on the broker (`.blrec.gz`, typically around
8x smaller than the raw capture); recordings saved before that change stay as
they are and keep working. Downloading still gives you plain `.blrec` JSONL
either way. The size shown in the library is the recording's own size, not its
compressed footprint -- hover it to see what it actually occupies on disk.

A recording is held in memory until you stop it — reloading the page discards
an in-progress recording.

## Long recordings roll over

A single recording holds 50 MB of captured output (or 250,000 recorded events,
whichever comes first). At that point capture does **not** stop: the segment so
far is saved and a fresh one opens immediately on the same terminal, seeded with
the screen as it stood at the boundary. The ⏺ timer restarts from 0:00 and the
elapsed clock is per segment.

Segments of the same run are listed in the library as **part 2/3** and so on,
newest first, and a player window titles the part it is playing. Each part is a
complete recording in its own right — it opens on the screen the previous part
ended on — so there is no seeking across a boundary.

## Playback

Open **Session recorder** from the right-click (+) menu to list recordings —
play, download (`.blrec`, newline-delimited JSON), or delete (click ✕ twice)
each one. **Play** opens a player window fixed at the recording's original
columns×rows — the window sizes itself to the recording, and follows any
resizes that happened during it.

## Several brokers

The library lists recordings from **every broker you have connected**, merged
into one list, and each row is tagged with the broker that holds it. Play,
download, notes and delete all act on that broker — nothing is copied between
them. The drop-down beside **Refresh** narrows the list to one broker; it
appears only when more than one is configured, and resets to *All brokers* when
the page is reloaded.

A broker that does not answer gets its **own row** rather than quietly
shortening the list: unreachable, or *password required* with a **Sign in**
button. Signing in refills the list in place. A broker running a build older
than the recorder reads as *no recorder on this broker*.

Two details worth knowing:

- The tag means **stored on**, not *ran on*. A recording is always saved to the
  broker this page is served from, even when the terminal itself is running on
  a remote one — so recording a remote session files it here.
- Hiding a broker (the per-broker hide toggle) masks its **windows**, not its
  recordings. They stay listed, so you can still delete them.

The merged list is ordered newest-first by each recording's start time, which
is taken from the clock of the browser that recorded it. Across machines with
disagreeing clocks that ordering is an approximation.

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
