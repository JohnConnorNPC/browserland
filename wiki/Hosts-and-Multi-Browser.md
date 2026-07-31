One Browserland desktop can attach to more than one broker at a time, so you can drive terminals on several machines from a single browser tab. This page covers adding remote hosts, reading their status chips, and the single-active-browser lease that keeps two browsers from fighting over the same layout.

## Add a remote host

By default the UI talks to the broker it was served from (`http://127.0.0.1:4445/`). To attach another broker, open **Control Panel → Hosts** and add it:

1. Enter a **label** (how the host appears in the UI).
2. Enter the broker **URL**.
3. Enter the broker's **password** — the broker's auth token, which doubles as its browser login. Every broker has one: either the `auth_token` its owner configured, or one it minted for itself on first run. Print it on that machine with `python -m webterm.broker --print-token`.

**Serve remote brokers over https** — the easy way is [`tailscale serve`](https://tailscale.com/kb/1312/serve) on each machine (real TLS inside your tailnet, no certificates to manage, the broker stays bound to loopback), or any https reverse proxy of your own. Two reasons: a browser blocks an **https page from talking to an http host** (mixed content), so the cockpit page and every added host must share one scheme — all https or all plain http, never a mix; and only an https (or localhost) page is a *secure context*, which clipboard **image paste** into a terminal (`Alt+V` / right-click) requires. All-plain-http over a trusted tailnet works too, minus those features.

Each host you add gets its own settings tab in the Control Panel. Settings like window mode, drag hold delay, MCP, keyboard shortcuts, the default terminal profile, and the default start path are stored **per host**, so they can differ from broker to broker. A separate set of **browser-global** settings (theme and background, terminal font, the start-button label, restore-on-refresh, when the broker status chip shows, the taskbar workspace filter, whether the workspace pager shows, and the clock chip's time zone) belong to the browser you are sitting at and are shared across every host. For more on opening the Control Panel and the rest of its tabs, see [[Getting-Started]].

One per-host setting is stored differently from all the others: **Clipboard (OSC 52)**, which decides whether programs running on that host may set your clipboard. It is off by default, it is authorised for one host at a time, and unlike every setting above it, it is kept in **your browser only** and never written to the broker. The reason is that it is precisely the switch that grants a broker access to the computer you are sitting at, so a copy of it living on that broker would let the broker turn it on for itself. Trusting your own laptop's broker therefore does not extend to a remote box you attached to, and enabling it in one browser does not enable it in another. See [[Keyboard-Shortcuts]] for what a program can and cannot do once it is on.

The password you enter is the broker's browser-login token. The bearer token AI agents use to drive terminals over MCP is a **separate** secret, configured on its own — if you plan to let agents work on this host, see [[MCP-and-AI-Agents]].

## Sharing your broker list

Your host list normally lives only in the browser you added it in: open the desktop from a different browser and you start with an empty list, and clearing local storage to fix a UI glitch also wipes your remote brokers. The optional **Broker registry** fixes that by storing a copy of the list on a broker, so you can pull it back somewhere else. It is a convenience layer, not a change to how connections work — your host list stays browser-local and remains the source of truth, and the desktop always connects **directly** to each broker. The broker only stores the list; it never proxies your traffic.

Find it in **Control Panel → Browser → Broker registry**, just below the Hosts list.

- **Publish** saves this browser's list to the broker. You pick which hosts to include and set the absolute address other machines use to reach this broker (a loopback address like `localhost` / `127.x` can't be shared, so the local broker is skipped when its address is loopback). Optionally publish to **every configured broker** at once, so opening the desktop from any of them recovers the full list; each broker is reported separately. Publishing requires this browser to hold the target broker's active-writer lease.
- **Pull** imports hosts published to this broker. New hosts are checked by default; a host you already have that differs is unchecked (your local copy wins unless you tick it), and an identical one is greyed out. Matching is by the broker's verified identity, then its address, so pulling an updated list updates the hosts you already have instead of duplicating them, and it never touches your **this broker** entry or your default-host choice. If you apply a host whose URL changed, its saved password is cleared (never carried across to a different address), so you may need to re-enter it.

**Passwords are not shared unless you ask.** The **Include passwords** box is off by default and warns you: publishing a broker's token lets anyone who can read that registry log into every included broker — a lateral-movement path between your machines. Only include passwords when the broker holding the registry is as trusted as the brokers whose tokens it carries. **Forget passwords** removes every token from the registry *and* its saved history, but it does **not** revoke access — a browser that already pulled a token keeps it, and the token stays valid until you rotate it (`--print-token` shows the current one). If a token was ever exposed, rotate it.

## Mods on a broker

Which mods run is normally **your browser's** choice: **Control Panel → this broker → Mods** lists everything installed and you tick what you want, and that choice stays in the browser you made it in. On a setup with several brokers that means the same decision has to be made again in every browser you sit at.

Each host tab therefore also has a **Mods on this broker** section — including the tab of a *remote* broker. It lists the mods **that** broker serves, each with three states:

- **Default** — that broker says nothing, so each browser's own Mods list decides. The option says which way "default" falls for that mod (`Default (on)` / `Default (off)`), because a few mods ship off.
- **On** / **Off** — that broker *pins* the mod. Every browser that loads its page gets that answer, and the mod's checkbox in their Mods list is locked and labelled `pinned on` / `pinned off`.

A few things worth knowing:

- **It applies at page load, not now.** A pin is what that broker hands to the *next* browser that loads its page — including your own tab if you pinned something on **this broker**, so a change here is not live in the session you make it in.
- **Pinning on is a real switch, not a suggestion.** It turns the mod on for everyone on that broker, including mods that ship off by default (the clipboard history, for instance). Pin one on only if you want it running in every browser that broker serves.
- **Pinning OFF is a real switch too.** Most mods ship on, including **Workspaces** (see [[Workspaces]]) — pinning that one off gives every browser on the broker a single desktop showing every tiled column at once. Nothing is lost by it: pin it back on and the workspaces return.
- **Pinning a mod on also pins what it needs.** The scratchpad needs the text editor, so pinning the scratchpad on shows the editor as `on — required by scratchpad`. If you pin a dependency **off** explicitly, that wins: the mod that needs it stays off and shows as blocked in the Mods list.
- **A pin lives on the broker**, not in your browser, and not in the settings that sync between your browsers. It survives a restart, and it can be changed even while somebody else's browser is the active view on that broker — unlike the other settings on a host tab, which that broker only accepts from its active browser.
- **A mod that broker doesn't have** still shows if it is pinned there (as `not installed on this broker`), so a leftover pin is visible and removable rather than silently governing nothing.

If a broker can't answer, the section says why instead of showing an empty list: it may be **running an older build** that can't report its mods (update it to manage them from here), **serving no desktop page** at all (a headless broker has no mods to pin), **unreachable**, or **refusing your password**. Select the tab again to retry.

The broker-wide **master switch** still outranks all of this: a broker started with `mods_enabled: false` runs no mods at all, and its section says so.

## Copying a mod setup between brokers

Setting the same mods up by hand on every machine is what **Control Panel → Browser → Sync mods** is for. It copies which mods are on, plus the settings those mods own, to the brokers you pick. It never sends mod **code** — a broker can only be told about mods it already serves, and one it doesn't have is reported rather than quietly skipped.

The two directions are deliberately not mirror images, because only one kind of per-mod state on another machine can be written from here at all:

- **Push to brokers…** writes each target's own **mod policy** (the pins above) and its **mod settings**. Pins are the only per-mod state on another machine that is remotely writable, and they are what that broker hands every browser that loads it.
- **Adopt from a broker…** changes **this browser's** own Mods choices and this broker's mod settings. It pins nothing here — a local pin would lock this browser's own checkboxes, and since pins are read once at page load it wouldn't even apply until you reloaded.

So pushing configures a machine; adopting configures you.

### Pins are kept to a minimum

By default a pin is written **only where that broker's own default would land somewhere else**, and a pin it no longer needs is **cleared**. Syncing two brokers that already agree therefore locks nothing, and pushing repeatedly doesn't pile up pins. Ticking **Lock every mod on the target** instead pins every shared mod explicitly — that locks those checkboxes for everyone on that broker, and it is the only way to also override a choice a browser over there made just for itself.

As everywhere else with pins: what you push applies the next time a browser **loads** that broker's page, not to a session already open there. Mod settings are shared state, so its browsers pick those up as they poll.

### Nothing is written before you have seen it

Selecting brokers only opens a **preview**, which names — per broker — every pin it will create or clear, every setting that will change with its old and new value, and everything being left alone and why. Brokers that already match say so and are unticked.

Afterwards each broker keeps its own result row: a push is never all-or-nothing. **Retry** rebuilds the whole plan against that broker as it is *now* rather than replaying a stale one, and **Undo pins** puts back the pins that broker had before that push (settings are not undone — their old values were that broker's, and re-asserting a whole blob is exactly the overwrite this avoids).

A broker whose mods can't be listed is **refused** rather than written to blind, since without its mod list there's no way to tell which settings it has any use for: an **older build** that can't report its mods (update it first), one **serving no desktop page**, one **unreachable**, or one **refusing your password**. A broker whose **master mod switch is off** takes the settings but no pins, because a pin made then is inert *and* awkward to clear later. A host you have no password for is shown but not selectable.

One limit worth knowing: only settings owned by a mod that is **on** in this browser travel, because a mod's settings control only exists while the mod is running. That's consistent — a mod that's off here is being turned off there too — but it means a value a disabled mod deliberately keeps (the terminal-font mod preserves your font so re-enabling restores it) stays behind. Turn the mod on, push, then turn it off again if you want that value to travel.

## Default color per host

Each host — including the local **this broker** — can carry an optional **default terminal color**. Set it in **Control Panel → Hosts** with the color dot on that host's row (the same swatch picker used in a window's title bar). When set, every **new** terminal launched on that host starts in that color instead of the automatic palette pick, so you can tell at a glance which broker a window belongs to. The host's status chip also gets a thicker border in that color.

The default is only a *starting* color: recoloring an individual window with its own title-bar picker still wins and sticks, and clearing the host default (the **✕** next to the dot) reverts new terminals to the automatic per-window colors. Like a host's password, the default color is stored in this browser only and is not shared with your other browsers.

## Default color per profile

A launch **profile** can also carry its own optional **default terminal color**, set in **Control Panel → (a host tab) → Terminals → Launch profiles** with the color dot on that profile's row. When set, every **new** terminal launched from that profile starts in that color — useful when the meaningful distinction is the profile rather than the host (say `prod-ssh` always red, `scratch` always green), regardless of which host runs it.

The profile color sits between the per-window and per-host colors in the order of precedence: a window you have recolored by hand keeps its own color; otherwise the launch profile's color wins; failing that the host's default color; and finally the automatic palette pick. Clearing it (the **✕** next to the dot) drops back to the host/auto colors. Unlike the per-host default (stored in your browser only), the profile color lives in the broker's profile definition, so it is shared with every browser and viewer of that broker.

## Default host for the START (+) button

A quick-launch of the START (**+**) button — a plain left-click by default — launches a terminal on your **default host**, using that host's own default terminal profile. Pick which host that is in **Control Panel → Hosts** with the **Default** button on that host's row; the current default is marked with a **default** badge and its own button is disabled. The local **this broker** is selectable too and is the default when you have not chosen one — so leaving it unset behaves exactly as before. (The button's picker menu still lets you launch on any host regardless of this setting.)

If you delete the host that was your START default, it falls back to **this broker** automatically, so the button keeps working. When the chosen host needs a password, quick-launching START surfaces its login prompt just like opening the host directly. Note that remote host identities are specific to the browser where you added them, so a non-local START default is only meaningful in that browser; other browsers fall back to launching locally.

## Host status chips

With a **single broker**, Browserland shows its status chip in the host-status area of the taskbar. The chip displays the host's label; its state tells you whether that host is reachable and whether this browser holds its lease:

| State | Meaning | Click does |
|---|---|---|
| ok | Reachable, and this browser is the active writer of its layout. | Hide / show that host's windows |
| down | Unreachable (broker down, or a pre-CORS version). | Hide / show that host's windows |
| auth-needed | Up, but the password is missing or wrong. | Open the login prompt |
| lease | Reachable, but another browser holds the active-writer lease. | Take over the lease |

With **two or more brokers**, the chips collapse into a single **aggregate badge** so they stop competing with the session buttons for taskbar width. The badge reads `N brokers`, colored by the worst state present (auth-needed, then down, then lease) — and it appends `· K need attention`, counting every host that is auth-needed, down, or lease, so one kind of fault never hides another. While first contact is still in flight it reads `· checking…`. Hovering the badge lists every host with its state; it is struck through only when every broker is hidden. **Click it** (or press Enter/Space — it is keyboard-focusable) to open the start (+) menu.

A broker **you hid yourself** is not counted and does not color the badge — hiding one is a choice, not a fault, so parking an offline machine no longer leaves the badge permanently red. Its real state is still there in the badge's hover list and on its row in the (+) menu, both of which mark it `— hidden`. If every broker is hidden, the badge drops its state color entirely and just goes struck through.

There, each broker has a **live status row** above its profiles: a state-colored dot (a per-host identity color becomes the dot's fill, with the state as its ring), the host's label, and a state suffix such as `— down` or `— password required`. The row performs the chip's action for its state — an **auth-needed** row opens the login prompt, a **lease** row requests the take-over, and an **ok**/**down** row toggles that broker's windows hidden or shown. The hide toggle **keeps the menu open** (the row goes struck through and gains `— hidden`), so you can sweep several brokers in one visit; hiding masks windows, it never closes them. The same toggle also lives as a **hidden** checkbox on each host's row in **Control Panel → Hosts**.

So the status surfaces stay interactive, not just indicators — and every recovery path that used to live on a chip (re-opening a cancelled login, taking back a lease, spotting a down broker) is one badge click away when several brokers are attached.

Because those recovery paths now live on the (+) menu rows rather than on the chip, the chip itself is optional. **Control Panel → Desktop → Broker status chip** offers **Always** (the default), **Only when a broker needs attention** — it appears when a broker goes unreachable, wants a password, or is taken over by another browser, and vanishes again once nothing is wrong — and **Never**. A broker you hid yourself never triggers it. Whichever you pick, the (+) menu rows and the **hidden** checkboxes under Control Panel → Hosts are unchanged, so logging in, taking over and un-hiding stay reachable with no chip at all. See [[Taskbar]].

## Single active browser (the lease)

A broker allows only **one active browser at a time** to be the WRITER of its layout. That permission is a *lease*: the browser holding it owns the window arrangement for that broker. This prevents two open tabs from overwriting each other's layout.

If you open the desktop in a second browser (or a second tab) while another already holds the lease, you won't see windows immediately. Instead you get a **Become active** prompt:

> another browser is active
>
> This broker allows one active browser at a time. Taking over will deactivate the other one.

Click **Become active** to take over the lease. The previously active browser is deactivated and shows the same prompt, so it can take the lease back later.

Note that terminals keep running regardless of which browser holds the lease — the shells live in the agents, not the browser. Taking over the lease only changes who is editing the layout, not what the terminals are doing. For what the terminals, notes, editors, file managers, and the (off-by-default) AI-provider status monitor actually are, see [[Window-Types]].
