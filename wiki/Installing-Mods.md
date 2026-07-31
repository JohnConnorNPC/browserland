A **mod** is a piece of the Browserland desktop — a taskbar chip, an app window, a Control Panel setting — and a broker can be handed a new one at runtime, without a restart. This page covers installing, uninstalling and managing mods from the Control Panel. For what a mod actually is, how one is written, and the trust it takes on, see [[Writing a Mod|Writing-a-Mod]].

## Where to find it

Open **Control Panel → Mods** on the broker's own tab (the broker this page is being served from — installing and uninstalling always act on that one broker, not a remote host you've added). The pane lists every mod that broker knows about, each with an enable checkbox, its provenance, and its live status.

## Installing a mod

Click **Install a mod…** and pick a mod's folder (or a single `.js` file) from your computer. Before anything is sent, you get an **install preview** — the exact files and manifest that will be uploaded, plus any advisory warnings (a missing field, a file over the size limit, and the like) so you can back out before committing to something the broker would reject anyway.

## Uninstalling a mod

Every installed mod's row carries an **Uninstall** button. **Shipped mods have no Uninstall button at all** — they are part of the broker's own program and can't be removed this way; only a mod installed at runtime can be. Uninstalling gives you the option to also delete the mod's data stored on that broker; without it, the mod's code and pin are removed but the server-side data it left behind stays.

## Reload to finish

**An install or uninstall does not take effect in the page you're looking at.** Every mod's script shares one execution scope with every other mod and with the desktop's own code, and JavaScript cannot re-declare or un-declare something already loaded — re-running a mod's script in the same page throws an error instead of updating it. So the broker takes the change immediately (the next browser to load its page sees it), but *this* tab needs a reload to pick it up. If you just installed something and it doesn't seem to be there yet, that's why — reload the page.

## Provenance: shipped or installed

Each row in the Mods pane carries a small **shipped** / **installed** badge. **Shipped** mods came in the broker's own program and were there before you touched anything. **Installed** mods arrived through this install feature — from you, or from whoever administers that broker. The badge is how you tell the two apart at a glance.

## `?nomods=1` — the rescue hatch

If an installed mod bricks the desktop — throws on load, breaks a shared piece of the page — you might not be able to reach the Control Panel to remove it. Add `?nomods=1` to the broker's URL by hand and reload: the desktop comes up with no mods fetched or run at all, shipped or installed, so you can get back into the Control Panel and sort things out. It's a one-time, read-only flag — nothing is saved, so leave it off the URL once you're done and mods load normally again.

## Installed but disabled is not contained

Unchecking a mod in the Mods pane stops it from running — but its code is still present in the page, still fetched, and its stylesheet is still live either way. Disabling is a way to turn a mod off, not a way to keep code you don't trust off your machine. Only uninstalling removes it.

## A mod is not sandboxed

Nothing above is a security boundary. A mod runs with the same authority as the rest of the desktop — it can read tokens, reach every broker route the UI itself uses, and touch the same page state as core. Installing a mod is a trust decision, the same as installing any other program: only install one you'd be willing to review. See [[Writing a Mod|Writing-a-Mod]] for the full picture.

## Mods that ship off by default

A handful of shipped mods start disabled and have to be switched on yourself, under **Control Panel → Mods**:

| Mod | Why it's off by default |
|---|---|
| Clipboard | Clipboard history can carry secrets, so nothing is captured until you opt in |
| AI status | Checking providers' status pages exposes the broker's egress IP to those hosts |
| Update check | The broker also needs its own operator-set switch before it will reach GitHub at all |
| Terminal font | An opt-in setting rather than something everyone needs on |

Turning one on is the same as enabling any other mod — tick its checkbox in the Mods pane.
