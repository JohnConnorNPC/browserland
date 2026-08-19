# Terminal funnels: the write / input / resize enumeration (#201, A57)

This is the **funnel-completeness precondition** for issue #201. Before
recorder's `term.write` / `term.resize` patches (`mods/recorder/recorder.js:534-535`,
`:636-641`) may be declared replaceable by `info.tapOutput` / `info.onResize`,
the paths by which bytes reach a terminal — or leave it for the wire — have to be
*enumerated from the shipped source*, not assumed. This document is that
enumeration; `tests/test_ui_assets.py` pins it mechanically (see "The gate",
below), so it cannot rot into a stale hand-written list.

Scope: the **core** fragments listed in `webterm/broker/ui.py::_ORDERED`. Mods
are out of scope by construction and are called out as a named limit.

## How the enumeration was produced

Every `*.js` fragment in `_ORDERED` is comment- and string-stripped, then scanned
for calls to `write` / `writeln` / `writeUtf8` / `paste` / `resize` / `input` /
`onData` / `onBinary` / `send`, on *any* receiver. Each hit is then classified.
The scan is deliberately broader than the terminal: a new `.send(` or `.write(`
anywhere in core shows up and must be classified before the suite goes green.

## Output funnel — bytes INTO the terminal

| Where | Receiver | What it carries | Hookable at a core call site? |
|---|---|---|---|
| `73_js_window_runtime.js` "connecting to …" banner | `win.term.write` | core-authored status text | yes |
| `73_js_window_runtime.js` ws `onopen` banner | `win.term.write` | core-authored status text | yes |
| `73_js_window_runtime.js` ws `onmessage`, binary frame | `win.term.write` | **PTY bytes** (`Uint8Array` from an `ArrayBuffer` frame) | yes |
| `73_js_window_runtime.js` ws `onmessage`, non-JSON frame | `win.term.write` | raw frame text (parse-failure fallback) | yes |
| `73_js_window_runtime.js` ws `onmessage`, `type:'output'` | `win.term.write` | **PTY bytes** (JSON `output` frame) | yes |
| `73_js_window_runtime.js` ws `onclose` | `win.term.write` | `[connection closed]` | yes |
| `73_js_window_runtime.js` ws `onerror` | `win.term.write` | `[ws error]` | yes |

Seven sites, one receiver: **every core write goes through the single
`Terminal` instance held at `win.term`**, and that instance is created at exactly
one place in core (`67_js_window_lifecycle.js`, `new Terminal({...})`). There is
no second write spelling in core — no `writeln`, no `writeUtf8`, no
`term._core` back door, no core `write` on a terminal core did not create.

Reattach/restore does **not** add a path: reattaching re-dials the WebSocket and
replays through the same `ws.onmessage` sites above; the active-view rebuild
(`84_js_active_view_lifecycle.js`) re-opens windows through `openWindow`, which
constructs a fresh `Terminal` and re-emits `registerTerminalCreate`. Nothing
replays a saved buffer into a terminal in core (the serialize addon is loaded in
`40_body.html` but core never instantiates it — that is recorder's).

## Input funnel — bytes OUT to the wire

| Where | Receiver | What it carries | Hookable at a core call site? |
|---|---|---|---|
| `67_js_window_lifecycle.js` `sendChunked` | `win.ws.send` | **the single wire chokepoint** for `type:'input'` — ≤256 Ki-char frames, surrogate-safe | yes |
| `67_js_window_lifecycle.js` `term.onData(...)` | `term.onData` | keystrokes, xterm's own automatic replies (DA1 etc.), and xterm-native pastes — all into `sendChunked('input', …)` | yes |
| `67_js_window_lifecycle.js` `pasteTextToTerm` | `term.paste` | clipboard / right-click / Ctrl+Shift+V / image-paste path text, when xterm's own bracketing applies — re-emerges through `onData` | yes (observed via `onData`) |

Every producer funnels into one of those two: `pasteTextToTerm` either calls
`term.paste` (→ `onData` → `sendChunked`) or, on the ConPTY `?2004` gap path,
calls `sendChunked('input', …)` with hand-written brackets directly. Alt+V's
fallback `ESC v`, the image-paste injected file path, the DOM `paste` capture
seam and the context-menu paste all reach the wire through those same two.
So an input tap placed on `sendChunked` sees **every** input byte core sends;
a tap on `onData` alone would miss the hand-bracketed ConPTY path.

Not terminal traffic, listed so the classification is exhaustive:
`64_js_sessions_poll_control.js` sends `{type:'ping'}` and
`{type:'become_active'}` control frames on session sockets. Those carry no
terminal bytes and are not part of the input funnel.

## Resize

| Where | Call | What it carries | Hookable? |
|---|---|---|---|
| `73_js_window_runtime.js` `resized` handler | `win.term.resize(cols, rows)` | the applied grid (broker-confirmed) | yes |
| `73_js_window_runtime.js` `sendResize` | `win.ws.send({type:'resize',cols,rows})` | the requested grid | yes |

Core measures cells itself (`readCellDims`) and requests a grid over the wire;
the terminal grid changes only when the broker echoes `resized`. Core loads the
fit addon (`term.loadAddon(fitAddon)`) but **never calls `fit()`** — see the
limits.

## Addons

`40_body.html` loads `xterm-addon-fit` and `xterm-addon-serialize`. Core
instantiates and loads only `FitAddon`, and calls no method on it. No core-loaded
addon writes bytes into a terminal.

## Named limits — paths a core call-site hook CANNOT see

These are recorded, not narrowed away. A tap built on core call sites is
complete *for core*; each item below is outside that boundary and must not be
described as covered.

1. **`fitAddon.fit()` called by a mod.** `mods/termfont/termfont.js:87` calls
   `win.fitAddon.fit()`. FitAddon calls `Terminal.resize` *inside xterm*, so the
   grid changes with no core call site involved. An `onResize` fed only from
   core's `win.term.resize` site will not fire for it.
2. **Mod-originated writes.** The `onTerminalCreate` info bag hands mods the
   concrete `win`, so any mod can call `win.term.write(...)` directly — recorder's
   playback does exactly this on its own terminals. Core cannot observe those from
   its own call sites.
3. **Writes that originate inside xterm.** Local echo, the reflow/redraw path,
   and the parser's own responses never pass through a core call site. (Their
   *replies* do leave through `onData`, so the input direction is covered; the
   write direction is not.)
4. **`term.paste()` bytes, pre-`onData`.** The paste text is observable as it
   leaves `onData`, chunked and possibly bracketed by xterm — not byte-identical
   to the clipboard text at the moment of the paste call.

Consequence for #201: `tapOutput` and `tapInput` can be declared complete for
core-originated traffic. `onResize` **could not** be declared complete while a
mod can drive a resize through the fit addon — and A58 closed that by taking the
second option rather than the first: `info.onResize` is fed from the vendored
build's own `Terminal.onResize` event (`vendor/xterm.js` exposes
`get onResize()`), which fires after the grid is applied *whoever* called
`resize` — core's call site, the fit addon, or a mod. So core's own
`win.term.resize` site deliberately dispatches nothing; feeding both would
double-report the wire path.

Limit 1 is therefore **closed for `onResize`**, and this paragraph records how
rather than deleting it. What remains is narrower and is the same containment
boundary as limit 2: a mod that replaces `term.resize` by assignment and never
calls through fires no event. Limits 2, 3 and 4 stand as written.

## What the gate does NOT cover

Two bounded scope limits, recorded so the word "complete" above stays honest:

1. **Only `.js` entries of `_ORDERED` are scanned.** `00_head.html`, `40_body.html`
   and `99_tail.html` are in `_ORDERED` and are never read by the scan. Checked
   at the time of writing: `40_body.html` ends by opening the single `<script>`
   and carries no inline JS, and `99_tail.html` is three lines — so there is no
   live hole. A `<script>` block added to one of those files would reach the
   page and the gate would not see it.
2. **The scan is textual, so computed access evades it.** `win.term['write'](d)`
   or `const w = win.term.write; w(d)` produce no match and leave the counts
   unchanged. Property access with a literal name does not evade it — both
   `win.term?.write(` and `wins[id].term.write(` produce a new key and fail the
   gate. So the doc's claim is precisely "no second write SPELLING in core",
   not "no possible write expression".

## The gate

`tests/test_ui_assets.py`:

* `test_terminal_funnel_enumeration_matches_the_shipped_fragments` — re-derives
  the scan above from the fragments on every run and compares it, counts and all,
  against the checked-in table. A new `.write(` / `.send(` / `.resize(` /
  `.paste(` / `.onData(` / `.onBinary(` call site anywhere in core fails it until
  someone classifies the new path as tappable or not. The list cannot go stale,
  because the list is never the input — the fragments are.
* `test_one_core_terminal_instance_is_the_write_funnel` — exactly one
  `new Terminal(` in core, and every enumerated write/resize site targets
  `win.term`, so a single per-instance tap point is sufficient.
* `test_core_loads_no_addon_that_writes` — the only core `loadAddon` is FitAddon,
  and core never calls `fit()`.
* `test_a_probe_byte_from_every_enumerated_write_site_reaches_one_tap` (node) —
  lifts each enumerated call *expression from the fragment source* and executes
  it against a recording stub installed on `win.term` / `win.ws`. Every site's
  probe is observed by that one tap. This is the "byte entering by any core path
  is seen by a registered tap" proof, run against the shipped text rather than a
  transcription of it.
