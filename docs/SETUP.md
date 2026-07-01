# Setup & onboarding

A practical guide to standing Browserland up — single machine first, then
multiple machines over [Tailscale](https://tailscale.com/) — written so a
**human or a coding agent** can follow it without guessing at the architecture.

If you are an AI agent helping someone set this up: read **[The mental
model](#the-mental-model)** first. Most setup mistakes come from confusing the
three roles below, or from hand-editing `broker_config.json` when the right
action was a click in the browser UI (see **[Don't hand-edit
this](#dont-hand-edit-this)**).

## The mental model

Browserland has exactly three roles. Keep them straight and everything else
follows.

| Role | What it is | Where it runs |
|---|---|---|
| **Broker** | A small web server. Serves the desktop UI, relays bytes, and spawns agents from pre-approved **profiles**. The authority. | **One per machine.** |
| **Agent** | A headless process that owns a real PTY (a terminal) and streams it to *a* broker over one WebSocket. | On the same machine as the broker that launches it. |
| **Browser** | The desktop UI. Renders windows, sends your keystrokes. Can attach to **several brokers at once**. | Wherever you point a browser. |

The shape that trips people up:

- **One broker per machine.** A broker is not a "client" you point at another
  machine. Each machine you want terminals on runs its own broker.
- **Agents are local to their broker.** You normally don't run an agent on
  machine A pointed at a broker on machine B. You run a broker on B, and B
  launches B's agents. (A remote agent *is* possible — see TECHNICAL.md — but
  it's the exception, not the multi-machine model.)
- **Brokers are joined in the browser, not in config.** To see machine B's
  terminals next to machine A's, you don't edit a config file — you open the UI
  and add B as a **host** (Control Panel → Hosts).

So the canonical two-machine setup is:

```
  Machine A                         Machine B
  ┌──────────────┐                  ┌──────────────┐
  │ broker A     │                  │ broker B     │
  │  └ agents A  │                  │  └ agents B  │
  └──────┬───────┘                  └──────┬───────┘
         │                                 │
         └──────────► browser ◄────────────┘
            (Control Panel → Hosts: adds B by its Tailscale IP)
```

## Single machine

Follow the **[Quick start](../README.md#quick-start)** in the README: install,
run the broker, open `http://127.0.0.1:4445/`, click **new terminal**. On a
single loopback machine you need no token and no config file — the defaults work.

You only need the rest of this page once a second machine, a network bind, or an
unattended broker enters the picture.

## Multiple machines over Tailscale

Goal: one browser tab showing terminals from every machine.

1. **Install Tailscale** on each machine so they share a private network. Note
   each machine's Tailscale IP (`tailscale ip -4`) or MagicDNS name.

2. **On *every* machine, run a broker** — one each, not a broker-plus-remote-
   agent. Two things every non-loopback broker needs:

   - **An `auth_token`.** Without a token the broker refuses non-loopback
     connections by design, and the UI's add-host form requires the password.
     Set it in `broker_config.json` (`"auth_token": "…"`) or via
     `$WEB_TERMINAL_TOKEN`.
   - **A non-loopback bind.** Set `"host"` to the machine's Tailscale IP (or
     `0.0.0.0` if you trust the tailnet) instead of `127.0.0.1`, so the other
     machine can reach it. Keep `"port": 4445` unless you have a reason.

   Treat your home machine as just another broker — it can stay on `127.0.0.1`
   if you only ever drive it from its own browser, but give it a token + tailnet
   bind too if you want to reach it from elsewhere.

3. **Open the UI on one machine** (`http://<that-machine>:4445/`). This is your
   "cockpit" — it doesn't have to be special, any broker's UI can host the view.

4. **Add the other brokers as hosts.** Control Panel → **Hosts** → add each
   remote broker by `http://<tailscale-ip>:4445/` and its token/password. The
   **browser connects directly** to each host (cross-origin `fetch` + WebSocket);
   hosts and passwords live per-browser in `localStorage`, never in any config
   file.

Per-host status chips appear in the taskbar (green ok / red down / amber
password-needed) once more than one host is configured. Requirements worth
knowing:

- **Both brokers must run the same webterm version** — a too-old remote shows up
  as a red "down" chip even while it's running (CORS is version-gated).
- **Serve plain http to plain-http remotes** — an https page fetching an http
  broker is blocked by the browser as mixed content. Over a tailnet, plain http
  between machines is normal.

Full auth/CORS details: **[TECHNICAL.md → Multiple hosts](TECHNICAL.md#multiple-hosts)**.

## Don't hand-edit this

The single most common setup mistake (especially when an AI agent is driving) is
editing the wrong thing. Rules of thumb:

- **Joining brokers is a UI action, not a config edit.** To attach machine B,
  use Control Panel → Hosts in the browser. There is **no** "remote brokers"
  list in `broker_config.json` — looking for one and inventing one is a dead end.
- **`broker_config.json` describes *this* broker only**: its bind (`host`/
  `port`), `auth_token`, MCP gates, and the launchable `profiles`. Nothing in it
  points at other machines.
- **`profiles` are an allow-list, deliberately.** The browser can only launch
  pre-approved profiles — it can never supply a raw command. The `agent.profiles`
  here are the **seed**; the easiest way to add a WSL/zsh/PowerShell profile is
  **Control Panel → Launch profiles** (add/edit/detect, applied live with no
  restart), which persists to a `webterm_profiles.json` sidecar that then owns
  the set. See **[PROFILES.md](PROFILES.md)** for copyable recipes and the
  sidecar-vs-`broker_config` rule.
- **Tokens/passwords for *remote* hosts live in the browser** (localStorage),
  set through the add-host form — not in any file on disk.

If you're an agent and you find yourself about to edit `broker_config.json` to
"connect to the other machine," stop: that's a Control Panel → Hosts action
instead.

## Running the broker in the background

A broker is a long-lived server; you usually want it running unattended and
surviving logout — not tied to a terminal you happened to launch it from.

### Windows (Task Scheduler — recommended)

Launching the broker from a **restricted or unusual parent process** (a sandbox,
a service with a stripped profile, or another tool's subprocess) can hand its
child terminals the wrong environment — bad permissions on user-profile paths
(PSReadLine history, Codex/agent state, application logs) and stray inherited
environment variables. The fix is to start the broker from a **clean, normal
user context**. Task Scheduler does exactly that.

Create a task that runs at logon as your own user:

```powershell
# adjust the path to your checkout and python
$action  = New-ScheduledTaskAction -Execute "python" `
             -Argument "-m webterm.broker" `
             -WorkingDirectory "X:\path\to\browserland"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
             -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "Browserland broker" `
  -Action $action -Trigger $trigger -Settings $settings `
  -RunLevel Limited                # run as a normal (non-elevated) user
```

Notes:

- **Run as the interactive user, non-elevated** (`-RunLevel Limited`). Running
  the broker elevated or as `SYSTEM` is what causes the profile-path permission
  problems above — its spawned terminals inherit that context.
- **Don't launch the broker from another tool's subprocess** if that tool
  injects its own environment. A broker started inside, say, a coding-agent
  session can leak that agent's marker variables into every terminal it spawns;
  Task Scheduler avoids this by starting from a clean session. If you must
  restart it from such a context, scrub the offending variables from the
  environment first.
- Set `auth_token` and a tailnet bind in `broker_config.json` **before** the
  task starts binding non-loopback (see [above](#multiple-machines-over-tailscale)).
- Verify it's up: open `http://127.0.0.1:4445/`, or
  `Get-ScheduledTask "Browserland broker"`.

Alternative: any non-restricted startup method works — a `.lnk` in
`shell:startup`, NSSM-as-a-service, etc. The requirement is just **clean,
non-elevated, normal-profile** — Task Scheduler is the least-surprising way to
get there.

### Linux (systemd)

Installable units ship in `launchers/systemd/`. See **[TECHNICAL.md → Linux
deployment](TECHNICAL.md#linux-deployment)** for the full walkthrough
(`webterm-broker.service` / `webterm-agent.service`, `User=`, paths, token).

## See also

- **[README → Quick start](../README.md#quick-start)** — install and first run.
- **[TECHNICAL.md](TECHNICAL.md)** — wire protocol, full auth/CORS model, every
  HTTP endpoint, multi-host internals, deployment.
- **[MCP & AI agent access](../README.md#mcp--ai-agent-access)** — letting an MCP
  client or harness drive the terminals.
