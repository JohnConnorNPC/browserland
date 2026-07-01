One Browserland desktop can attach to more than one broker at a time, so you can drive terminals on several machines from a single browser tab. This page covers adding remote hosts, reading their status chips, and the single-active-browser lease that keeps two browsers from fighting over the same layout.

## Add a remote host

By default the UI talks to the broker it was served from (`http://127.0.0.1:4445/`). To attach another broker, open **Control Panel → Hosts** and add it:

1. Enter a **label** (how the host appears in the UI).
2. Enter the broker **URL**.
3. Enter the broker's **password** — the broker's configured auth token, which doubles as its browser login.

Each host you add gets its own settings tab in the Control Panel. Settings like window mode, drag hold delay, MCP, keyboard shortcuts, the default terminal profile, and the default start path are stored **per host**, so they can differ from broker to broker. A separate set of **browser-global** settings (theme and background, terminal font, the start-button label, restore-on-refresh, the taskbar workspace filter, and the clock chip's time zone) belong to the browser you are sitting at and are shared across every host. For more on opening the Control Panel and the rest of its tabs, see [[Getting-Started]].

The password you enter is the broker's browser-login token. The bearer token AI agents use to drive terminals over MCP is a **separate** secret, configured on its own — if you plan to let agents work on this host, see [[MCP-and-AI-Agents]].

## Default color per host

Each host — including the local **this broker** — can carry an optional **default terminal color**. Set it in **Control Panel → Hosts** with the color dot on that host's row (the same swatch picker used in a window's title bar). When set, every **new** terminal launched on that host starts in that color instead of the automatic palette pick, so you can tell at a glance which broker a window belongs to. The host's status chip also gets a thicker border in that color.

The default is only a *starting* color: recoloring an individual window with its own title-bar picker still wins and sticks, and clearing the host default (the **✕** next to the dot) reverts new terminals to the automatic per-window colors. Like a host's password, the default color is stored in this browser only and is not shared with your other browsers.

## Default color per profile

A launch **profile** can also carry its own optional **default terminal color**, set in **Control Panel → (a host tab) → Launch profiles** with the color dot on that profile's row. When set, every **new** terminal launched from that profile starts in that color — useful when the meaningful distinction is the profile rather than the host (say `prod-ssh` always red, `scratch` always green), regardless of which host runs it.

The profile color sits between the per-window and per-host colors in the order of precedence: a window you have recolored by hand keeps its own color; otherwise the launch profile's color wins; failing that the host's default color; and finally the automatic palette pick. Clearing it (the **✕** next to the dot) drops back to the host/auto colors. Unlike the per-host default (stored in your browser only), the profile color lives in the broker's profile definition, so it is shared with every browser and viewer of that broker.

## Default host for the START (+) button

A quick-launch of the START (**+**) button — a plain left-click by default — launches a terminal on your **default host**, using that host's own default terminal profile. Pick which host that is in **Control Panel → Hosts** with the **Default** button on that host's row; the current default is marked with a **default** badge and its own button is disabled. The local **this broker** is selectable too and is the default when you have not chosen one — so leaving it unset behaves exactly as before. (The button's picker menu still lets you launch on any host regardless of this setting.)

If you delete the host that was your START default, it falls back to **this broker** automatically, so the button keeps working. When the chosen host needs a password, quick-launching START surfaces its login prompt just like opening the host directly. Note that remote host identities are specific to the browser where you added them, so a non-local START default is only meaningful in that browser; other browsers fall back to launching locally.

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

Note that terminals keep running regardless of which browser holds the lease — the shells live in the agents, not the browser. Taking over the lease only changes who is editing the layout, not what the terminals are doing. For what the terminals, notes, editors, file managers, and the (off-by-default) AI-provider status monitor actually are, see [[Window-Types]].
