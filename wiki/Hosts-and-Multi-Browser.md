One Browserland desktop can attach to more than one broker at a time, so you can drive terminals on several machines from a single browser tab. This page covers adding remote hosts, reading their status chips, and the single-active-browser lease that keeps two browsers from fighting over the same layout.

## Add a remote host

By default the UI talks to the broker it was served from (`http://127.0.0.1:4445/`). To attach another broker, open **Control Panel → Hosts** and add it:

1. Enter a **label** (how the host appears in the UI).
2. Enter the broker **URL**.
3. Enter the broker's **password** — the broker's configured auth token, which doubles as its browser login.

Each host you add gets its own settings tab in the Control Panel. Settings like window mode, drag hold delay, MCP, keyboard shortcuts, the default terminal profile, and the default start path are stored **per host**, so they can differ from broker to broker. A separate set of **browser-global** settings (theme and background, terminal font, the start-button label, restore-on-refresh, and the taskbar workspace filter) belong to the browser you are sitting at and are shared across every host. For more on opening the Control Panel and the rest of its tabs, see [[Getting-Started]].

The password you enter is the broker's browser-login token. The bearer token AI agents use to drive terminals over MCP is a **separate** secret, configured on its own — if you plan to let agents work on this host, see [[MCP-and-AI-Agents]].

## Host status chips

Browserland shows one status chip per broker — always, even for a single healthy local broker — in the host-status area of the taskbar. The chip displays the host's label; its state tells you whether that host is reachable and whether this browser holds its lease:

| State | Meaning | Click does |
|---|---|---|
| ok | Reachable, and this browser is the active writer of its layout. | Hide / show that host's windows |
| down | Unreachable (broker down, or a pre-CORS version). | Hide / show that host's windows |
| auth-needed | Up, but the password is missing or wrong. | Open the login prompt |
| lease | Reachable, but another browser holds the active-writer lease. | Take over the lease |

So the chips are interactive, not just indicators: an **auth-needed** chip is click-to-log-in, a **lease** chip is click-to-take-over, and an **ok**/**down** chip toggles whether that host's windows are shown or hidden in your desktop.

## Single active browser (the lease)

A broker allows only **one active browser at a time** to be the WRITER of its layout. That permission is a *lease*: the browser holding it owns the window arrangement for that broker. This prevents two open tabs from overwriting each other's layout.

If you open the desktop in a second browser (or a second tab) while another already holds the lease, you won't see windows immediately. Instead you get a **Become active** prompt:

> another browser is active
>
> This broker allows one active browser at a time. Taking over will deactivate the other one.

Click **Become active** to take over the lease. The previously active browser is deactivated and shows the same prompt, so it can take the lease back later.

Note that terminals keep running regardless of which browser holds the lease — the shells live in the agents, not the browser. Taking over the lease only changes who is editing the layout, not what the terminals are doing. For what the terminals, notes, editors, and file managers actually are, see [[Window-Types]].
