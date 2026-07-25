The Broker registry is an optional way to share your **host list** — the brokers you connect to in **Control Panel → Browser → Hosts** — through a broker, so you can recover it in another browser or after clearing this browser's data. It is a convenience layer, not a change to how connections work: your host list still lives in this browser and stays the source of truth, and the desktop always connects **directly** to each broker. The broker only stores a copy of the list; it never proxies your traffic.

You will find it in **Control Panel → Browser → Broker registry**, right below the Hosts list it manages. For adding and editing the hosts themselves, see [[Hosts-and-Multi-Browser]].

## Why you might want it

The host list normally lives only in this browser's local storage. That means opening the desktop from a different browser starts with an empty list, and clearing local storage to fix a UI glitch also wipes your remote brokers. Publishing the list to a broker gives you a copy you can pull back from anywhere that can reach that broker.

## Publish

Click **Publish…** to save this browser's host list to the broker. The dialog lets you:

- **Choose which hosts** to include (all are checked by default).
- **Set this broker's address** — the absolute URL other machines use to reach it. A loopback address (`localhost` / `127.x`) can't be reached from another machine, so the local broker is skipped when its address is loopback.
- **Also publish to every configured broker** — writes the same list to each broker you have, so opening the desktop from *any* of them can recover the full list. Each broker is reported separately; if one can't be written (for example another browser holds its active-writer lease), you are told which.

Publishing needs this browser to be the **active** browser for the target broker (the same single-writer lease that governs the layout). A non-active browser is told to take over first.

## Pull

Click **Pull…** to import hosts that were published to this broker. Each entry is classified against what you already have:

- **new** — a broker you don't have yet (checked by default).
- **differs** — a broker you already have, but with a different label, URL, or color (unchecked by default: your local copy wins unless you tick it). If the URL differs, applying it replaces your local URL **and clears that host's saved password**, so you may need to re-enter it — a saved password is never carried over to a different address.
- **already have** — identical to what you have (greyed out, nothing to do).

Matching is by the broker's verified identity first, then its address, so pulling an updated list **updates** the hosts you already have instead of duplicating them. The registry's own copy of a broker never replaces your **this broker** entry, and your default-host choice is left untouched.

## Passwords

Passwords (broker tokens) are **not** published unless you tick **Include passwords**, which is off by default and shows a warning. Publishing a token is a real risk: anyone who can read that broker's registry can then log into every broker whose token you included — a lateral-movement path between your machines. Only include passwords when the broker holding the registry is as trusted as the brokers whose tokens it will carry.

## Forget passwords

**Forget passwords** removes every token from this broker's registry, including its saved revision history, so a token can't be read back from an older stored version. It does **not** revoke access: a browser that already pulled a token keeps it, and the broker's token stays valid until you rotate it. If a token was exposed, change it on that broker (`python -m webterm.broker --print-token` shows the current one). If the broker is an older version that can't clear its history, you are told so you can rotate the token instead.
