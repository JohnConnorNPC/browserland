# Launch profiles

A **profile** is a named shell recipe the broker is allowed to spawn. The
browser (and any MCP client) launches a terminal by sending a profile **name** —
never a command. The broker looks the name up in its allow-list and spawns the
argv it finds there. This is deliberate: a client can *only* start pre-approved
shells, so an untrusted page or agent can never turn `/launch` into
"run arbitrary command X" (the RCE-by-design boundary).

Profiles power the right-click **`+`** launch picker, the per-host **default
terminal profile**, and every MCP `launch_terminal` call.

## The fields

Each profile is an object under a short name:

```jsonc
"ubuntu": {
  "command": ["wsl.exe", "-d", "Ubuntu", "--cd", "~", "--", "bash", "-l"],
  "title": "Ubuntu (WSL)",   // optional — the window title; defaults to the name
  "cwd": null,                // optional — the shell's start dir; null = agent default
  "color": "#e06666"          // optional — default terminal color (#rrggbb); null = none
}
```

- **`command`** (required) — the argv the broker runs, as a **list of separate
  tokens** (never one shell string). `["bash", "-l"]`, not `"bash -l"`. This is
  the whole point of the allow-list: no shell parses it, so there is nothing to
  quote-inject.
- **`title`** (optional) — the window title. Omitted → the profile name.
- **`cwd`** (optional) — a starting directory for that shell. Omitted/`null` →
  the agent's default. (A client may *also* pass a `cwd` at launch time; that is
  validated as an existing directory separately.)
- **`color`** (optional) — a default terminal **color** as `#rrggbb`, seeding
  every **new** terminal launched from this profile (window frame + taskbar
  chip). Omitted/`null` → no profile color, so the terminal falls back to the
  per-host default color, then the automatic palette pick. A per-window recolor
  from the title bar still wins and sticks. Unlike the per-host default (browser-
  local), this lives in the broker's profile, so every browser/viewer sees it.

## Where profiles live

Two sources, in order:

1. **`broker_config.json` → `agent.profiles`** — the **seed**. Read-only at
   runtime; ships the out-of-the-box defaults for the host OS.
2. **`webterm_profiles.json`** — the **sidecar**, written by the Control Panel
   editor, sitting next to the `/state` store (override with
   `profiles_state_path`). Atomic-write, like `webterm_mcp.json` /
   `webterm_state.json`.

**Sidecar-owns-once-written:** the first time you save a profile from the UI, the
sidecar is created and from then on it owns the *whole* set — `agent.profiles`
becomes just the seed. So if you hand-edit `broker_config.json` after that and
see no change, that is why (the broker logs it loudly at startup). **To go back
to the `broker_config.json` seed, delete `webterm_profiles.json` and restart.**
A missing, corrupt, or empty sidecar always falls back to the seed — it can never
brick startup or leave zero launchable shells.

## Editing profiles (no restart)

**Control Panel → (a host tab) → Launch profiles.** You can:

- **Add profile** — name, optional title, optional cwd, and the command as a
  textarea with **one argv token per line**.
- **Edit** / **Delete** / **Make default** an existing profile.
- **Detect…** — one-click environment scan that seeds the Add dialog:
  - **Windows** → your installed **WSL distros** (`wsl.exe -l -q`).
  - **Linux/macOS** → the shells actually present (`bash`, `zsh`, `fish`, `sh`).

Every save **applies immediately** — the live launcher swaps and the next launch
(and the `+` picker) uses the new set, no broker restart. A profile whose
`command[0]` isn't found on `PATH` is flagged **⚠ not found**.

## Security model

Editing profiles means defining commands the host will run, so the editor is
**browser-realm only**, gated exactly like `/file/*` and `/state` — a valid
token, on every interface, no exceptions. The endpoints:

- `GET /profiles/config` / `POST /profiles/config` — the **full** objects
  (commands included). Browser realm only.
- `GET /profiles/detect` — the environment scan. Browser realm only.
- `GET /profiles` and `GET /mcp/profiles` — **names only**, unchanged. An MCP/AI
  agent can list profile *names* to launch, but can never read a command or
  define a new profile.

Writing a profile is **no more powerful than `/file/write`**, which the same gate
already grants (both let an authenticated caller make the host run code). Since
#142 there is no tokenless broker and no loopback exemption, so a cross-origin
page — which carries no token — cannot reach these endpoints at all. That closes
the old caveat about running a tokenless broker while the same browser visits
untrusted sites.

## Recipe catalog

Copy a `command` into the Add-profile dialog (one token per line) or into
`broker_config.json`'s `agent.profiles`.

### Windows → WSL

```jsonc
// Ubuntu (Detect… fills these in for every installed distro)
"ubuntu": { "command": ["wsl.exe", "-d", "Ubuntu",  "--cd", "~", "--", "bash", "-l"] },
"debian": { "command": ["wsl.exe", "-d", "Debian",  "--cd", "~", "--", "bash", "-l"] },
"kali":   { "command": ["wsl.exe", "-d", "kali-linux", "--cd", "~", "--", "bash", "-l"] }
```

Swap `Ubuntu`/`Debian`/`kali-linux` for the exact name from `wsl.exe -l -q`.
`--cd ~` starts in the distro's home; drop it to inherit the broker's cwd.

### Windows → PowerShell / cmd / Git-Bash

```jsonc
"pwsh":       { "command": ["pwsh", "-NoLogo"] },                 // PowerShell 7+
"powershell": { "command": ["powershell.exe", "-NoLogo", "-NoProfile"] },
"cmd":        { "command": ["cmd.exe"] },
"git-bash":   { "command": ["C:\\Program Files\\Git\\bin\\bash.exe", "-l", "-i"] }
```

`-NoProfile` skips your PowerShell profile scripts (faster, cleaner). Adjust the
Git-Bash path to your install.

### Linux / macOS

```jsonc
"bash": { "command": ["bash", "-l"] },
"zsh":  { "command": ["zsh",  "-l"] },
"fish": { "command": ["fish", "-l"] },
"sh":   { "command": ["sh",   "-l"] }
```

`-l` makes it a login shell (sources your profile / rc). `Detect…` proposes each
of these that is actually installed.

### Launching an app instead of a shell

Nothing says `command` has to be a shell. A profile can launch a TUI or an agent
CLI directly, so the window *is* the app:

```jsonc
// A TUI, straight into the window — no shell in between.
"btop":    { "command": ["btop"], "title": "btop" },
"lazygit": { "command": ["wsl.exe", "-d", "Ubuntu", "--cd", "~", "--", "lazygit"] },

// Needs login-shell PATH (nvm / pyenv / ~/.local/bin) before the name resolves:
// -l sources your rc files, -c runs the app, and `exec` keeps the shell out of
// the way so quitting the app ends the session instead of dropping to a prompt.
"claude":  { "command": ["wsl.exe", "-d", "Ubuntu", "--cd", "~", "--",
                         "bash", "-lc", "exec claude"], "color": "#c96442" },

// A remote TUI: -t forces a PTY, without which curses apps refuse to start.
"htop-lab": { "command": ["ssh", "-t", "user@lab", "htop"] }
```

A profile that launches an app behaves differently from one that launches a shell,
in ways worth knowing before you wonder what broke:

- **There is no prompt to come back to.** When the app exits, the session exits
  with it and the window closes. A shell profile survives the app and leaves you
  a prompt; this does not.
- **The mouse is usually gone from the first frame.** Most full-screen apps turn
  on mouse reporting during startup, so drag-select is dead before you ever get a
  chance to try it. `Shift`-drag (`Option`-drag on macOS) forces a selection and
  `Shift+scroll` still reaches scrollback — see
  [Keyboard Shortcuts](https://github.com/JohnConnorNPC/browserland/wiki/Keyboard-Shortcuts).
  The **Mouse mode chip** mod puts a 🖱 in the title bar while that is the case.
- **No shell means no rc files.** `["claude"]` only works if `claude` is already
  on the broker's own `PATH`; anything installed by a login shell needs the
  `bash -lc "exec …"` form above.
- **`Detect…` will not propose these.** It scans for shells and WSL distros, so
  app profiles are always hand-added.

---

See also: **[SETUP.md](SETUP.md)** (config basics) and
**[TECHNICAL.md](TECHNICAL.md)** (endpoint reference, auth model).
