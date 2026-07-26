One Browserland desktop can attach to more than one broker at a time, so you can drive terminals on several machines from a single browser tab. This page covers adding remote hosts, reading their status chips, and the single-active-browser lease that keeps two browsers from fighting over the same layout.

## Add a remote host

By default the UI talks to the broker it was served from (`http://127.0.0.1:4445/`). To attach another broker, open **Control Panel → Hosts** and add it:

1. Enter a **label** (how the host appears in the UI).
2. Enter the broker **URL**.
3. Enter the broker's **password** — the broker's auth token, which doubles as its browser login. Every broker has one: either the `auth_token` its owner configured, or one it minted for itself on first run. Print it on that machine with `python -m webterm.broker --print-token`.

**Serve remote brokers over https** — the easy way is [`tailscale serve`](https://tailscale.com/kb/1312/serve) on each machine (real TLS inside your tailnet, no certificates to manage, the broker stays bound to loopback), or any https reverse proxy of your own. Two reasons: a browser blocks an **https page from talking to an http host** (mixed content), so the cockpit page and every added host must share one scheme — all https or all plain http, never a mix; and only an https (or localhost) page is a *secure context*, which clipboard **image paste** into a terminal (`Alt+V` / right-click) requires. All-plain-http over a trusted tailnet works too, minus those features.

Each host you add gets its own settings tab in the Control Panel. Settings like window mode, drag hold delay, MCP, keyboard shortcuts, the default terminal profile, and the default start path are stored **per host**, so they can differ from broker to broker. A separate set of **browser-global** settings (theme and background, terminal font, the start-button label, restore-on-refresh, the taskbar workspace filter, and the clock chip's time zone) belong to the browser you are sitting at and are shared across every host. For more on opening the Control Panel and the rest of its tabs, see [[Getting-Started]].

The password you enter is the broker's browser-login token. The bearer token AI agents use to drive terminals over MCP is a **separate** secret, configured on its own — if you plan to let agents work on this host, see [[MCP-and-AI-Agents]].

## Sharing your broker list

Your host list normally lives only in the browser you added it in: open the desktop from a different browser and you start with an empty list, and clearing local storage to fix a UI glitch also wipes your remote brokers. The optional **Broker registry** fixes that by storing a copy of the list on a broker, so you can pull it back somewhere else. It is a convenience layer, not a change to how connections work — your host list stays browser-local and remains the source of truth, and the desktop always connects **directly** to each broker. The broker only stores the list; it never proxies your traffic.

Find it in **Control Panel → Browser → Broker registry**, just below the Hosts list.

- **Publish** saves this browser's list to the broker. You pick which hosts to include and set the absolute address other machines use to reach this broker (a loopback address like `localhost` / `127.x` can't be shared, so the local broker is skipped when its address is loopback). Optionally publish to **every configured broker** at once, so opening the desktop from any of them recovers the full list; each broker is reported separately. Publishing requires this browser to hold the target broker's active-writer lease.
- **Pull** imports hosts published to this broker. New hosts are checked by default; a host you already have that differs is unchecked (your local copy wins unless you tick it), and an identical one is greyed out. Matching is by the broker's verified identity, then its address, so pulling an updated list updates the hosts you already have instead of duplicating them, and it never touches your **this broker** entry or your default-host choice. If you apply a host whose URL changed, its saved password is cleared (never carried across to a different address), so you may need to re-enter it.

**Passwords are not shared unless you ask.** The **Include passwords** box is off by default and warns you: publishing a broker's token lets anyone who can read that registry log into every included broker — a lateral-movement path between your machines. Only include passwords when the broker holding the registry is as trusted as the brokers whose tokens it carries. **Forget passwords** removes every token from the registry *and* its saved history, but it does **not** revoke access — a browser that already pulled a token keeps it, and the token stays valid until you rotate it (`--print-token` shows the current one). If a token was ever exposed, rotate it.

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

With a **single broker**, Browserland shows its status chip in the host-status area of the taskbar — always, even for a healthy local broker. The chip displays the host's label; its state tells you whether that host is reachable and whether this browser holds its lease:

| State | Meaning | Click does |
|---|---|---|
| ok | Reachable, and this browser is the active writer of its layout. | Hide / show that host's windows |
| down | Unreachable (broker down, or a pre-CORS version). | Hide / show that host's windows |
| auth-needed | Up, but the password is missing or wrong. | Open the login prompt |
| lease | Reachable, but another browser holds the active-writer lease. | Take over the lease |

With **two or more brokers**, the chips collapse into a single **aggregate badge** so they stop competing with the session buttons for taskbar width. The badge reads `N brokers`, colored by the worst state present (auth-needed, then down, then lease) — and it appends `· K need attention`, counting **every** host that is auth-needed, down, or lease, so one kind of fault never hides another. While first contact is still in flight it reads `· checking…`. Hovering the badge lists every host with its state; it is struck through only when every broker is hidden. **Click it** (or press Enter/Space — it is keyboard-focusable) to open the start (+) menu.

There, each broker has a **live status row** above its profiles: a state-colored dot (a per-host identity color becomes the dot's fill, with the state as its ring), the host's label, and a state suffix such as `— down` or `— password required`. The row performs the chip's action for its state — an **auth-needed** row opens the login prompt, a **lease** row requests the take-over, and an **ok**/**down** row toggles that broker's windows hidden or shown. The hide toggle **keeps the menu open** (the row goes struck through and gains `— hidden`), so you can sweep several brokers in one visit; hiding masks windows, it never closes them. The same toggle also lives as a **hidden** checkbox on each host's row in **Control Panel → Hosts**.

So the status surfaces stay interactive, not just indicators — and every recovery path that used to live on a chip (re-opening a cancelled login, taking back a lease, spotting a down broker) is one badge click away when several brokers are attached.

## Single active browser (the lease)

A broker allows only **one active browser at a time** to be the WRITER of its layout. That permission is a *lease*: the browser holding it owns the window arrangement for that broker. This prevents two open tabs from overwriting each other's layout.

If you open the desktop in a second browser (or a second tab) while another already holds the lease, you won't see windows immediately. Instead you get a **Become active** prompt:

> another browser is active
>
> This broker allows one active browser at a time. Taking over will deactivate the other one.

Click **Become active** to take over the lease. The previously active browser is deactivated and shows the same prompt, so it can take the lease back later.

Note that terminals keep running regardless of which browser holds the lease — the shells live in the agents, not the browser. Taking over the lease only changes who is editing the layout, not what the terminals are doing. For what the terminals, notes, editors, file managers, and the (off-by-default) AI-provider status monitor actually are, see [[Window-Types]].
