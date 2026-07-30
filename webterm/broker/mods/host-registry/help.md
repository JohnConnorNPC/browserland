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

## Encryption

The broker stores the registry as plain JSON in a file next to its state file, plus a short revision history. **Broker registry encryption** encrypts that value *in this browser* before it is published, so the broker only ever receives ciphertext. It is set in **Control Panel → Browser → Broker registry encryption**, and it has three modes:

- **Passwords only** (the default) — the labels and addresses stay readable; only the passwords are encrypted. Nothing changes until a publish actually carries a password, so with **Include passwords** off this mode does nothing at all and never asks for a passphrase.
- **Whole list** — the entire list is encrypted. The broker cannot see which machines you have, and the list is unreadable until you unlock it.
- **Off** — the old behaviour: everything is published in the clear.

Publishing asks for a passphrase, and pulling asks for it again to unlock. The passphrase is held in memory for as long as the page is open — so publishing to every broker, or pulling twice in a row, only asks once — and it is **never stored anywhere**. There is no "remember on this browser": a key kept in local storage is worth exactly as much as the passphrase to anything that can read local storage. **Forget passphrase** drops it. If you lose it, publish the list again with a new one.

### What this protects you from — and what it does not

It protects the registry **at rest**, and from anyone who can **read** the store: that file, a backup or snapshot of it, the revision history, another user or administrator of that machine, or a second browser that has the broker's token but not your passphrase.

It does **not** protect you from a **compromised or hostile broker**. That broker serves the very code that does the encrypting, so it could serve a version that keeps your passphrase. Encryption makes publishing passwords safe against someone reading the broker's disk; it does not make it safe against the broker itself. If you don't trust the broker, don't publish passwords to it.

It also cannot stop somebody who can *write* the registry from deleting it, or from replacing it with a list of their own. What it does stop is a subtler trick: because each password is encrypted **together with** the address it belongs to, nobody can edit the readable half to point one of your saved passwords at a machine of theirs. If the readable half and the encrypted contents disagree, you are told, and the encrypted (authenticated) list is the one used.

### Encryption needs a secure context

Browsers only expose the crypto this uses on a **secure context** — `https://`, or `localhost`. The broker terminates no TLS itself, so a broker reached at `http://192.168.x.x:4445` cannot encrypt. When that is the case the option says so, and publishing is **refused** rather than quietly falling back to the clear. To publish from such a page, either reach the broker over https (or on localhost), or set encryption to **Off** — which publishes in the clear, exactly as before.

### Other things worth knowing

- **Publishing to every broker** encrypts once and writes the same ciphertext everywhere, so all of those brokers share one passphrase.
- **The revision history is cleared** on every encrypted publish. Otherwise the plaintext copy you just replaced would still be sitting in the history of the store you had stopped trusting.
- A wrong passphrase is reported as a wrong passphrase and changes nothing locally — you can simply try again. (It can also mean the stored copy was modified; the two are the same failure and cannot be told apart.)
- **Older versions of Browserland** don't know about encryption. In *passwords only* mode they still read the list and import the labels and addresses, just without the passwords — the same as a list published without **Include passwords**. In *whole list* mode they report the registry as empty.

## Forget passwords

**Forget passwords** removes every token from this broker's registry, including its saved revision history, so a token can't be read back from an older stored version. It does **not** revoke access: a browser that already pulled a token keeps it, and the broker's token stays valid until you rotate it. If a token was exposed, change it on that broker (`python -m webterm.broker --print-token` shows the current one). If the broker is an older version that can't clear its history, you are told so you can rotate the token instead.

It works **without the passphrase** — that is the whole point of an emergency purge. Because an encrypted block is opaque, it is assumed to contain passwords and is removed whole. On a *passwords only* registry that leaves the readable list intact; on a *whole list* registry the list is inside the encrypted block, so removing the passwords means removing the list, and the confirmation says so before you agree to it.
