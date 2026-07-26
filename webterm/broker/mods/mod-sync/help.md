Sync mods copies **this broker's mod setup** — which mods are on, and the settings those mods own — to the other brokers you have configured, so the same choices don't have to be made by hand on every machine. You will find it in **Control Panel → Browser → Sync mods**, next to the Broker registry.

It never sends mod **code**. A broker can only be told about mods it already serves; a mod it does not have is reported rather than silently skipped. For adding the brokers themselves, see [[Hosts-and-Multi-Browser]].

## Why the two directions are not mirror images

Which mods run is normally **your browser's** choice, kept in that browser only. There is no way to reach into another browser's choices from here — so the two directions write different things, and it is worth knowing which:

- **Push** writes the target broker's **mod policy** (the pins described in [[Hosts-and-Multi-Browser]]) plus its **mod settings**. Pins are what a broker hands every browser that loads its page, and they are the only per-mod state on another machine that can be written remotely.
- **Adopt** changes **this browser's** own Mods choices and this broker's mod settings. It pins nothing here.

So pushing configures a machine; adopting configures you.

## Push

**Push to brokers…** lists every other broker you have configured. A broker with no saved password, or one that refused the password you have, is shown but cannot be selected — set its password on the **Browser** tab first.

Nothing is written until you have seen the preview. It names, per broker, every pin that will be created or cleared, every setting that will change (with its old and new value), and everything that is being left alone and why. Brokers that already match are shown as such and unticked.

Afterwards each broker gets its own result row in the pane — a push is never all-or-nothing — with **Retry** (which rebuilds the whole plan against that broker as it is *now*, rather than replaying a stale one) and **Undo pins**, which puts back the pins that broker had before the push.

### Pins are kept to a minimum

By default a pin is written **only where that broker's own default would land somewhere else**, and a pin it no longer needs is **cleared**. Syncing two brokers that already agree therefore locks nothing, and repeated pushes don't accumulate pins.

Ticking **Lock every mod on the target** instead pins every shared mod explicitly. That locks those checkboxes for every browser on that broker, and it is the only way to also override a choice a browser over there made locally.

### Timing

A mod pin takes effect the next time a browser **loads** that broker's page — not immediately, and not in a session that is already open there. Mod settings are shared state, so its browsers pick those up as they poll.

### When a broker can't be pushed to

Each case gets its own row rather than a generic failure:

- **Older build** — it does not report its mods, so its setup can't be read or changed from here. Nothing is written at all: without its list of mods there is no way to tell which settings it has any use for. Update it first.
- **Serves no desktop page** — a headless broker runs no mods, so nothing here applies to it.
- **Master mod switch off** — it runs no mods at all (`mods_enabled: false`). Settings are still written; pins are not, because a pin made now would be inert *and* awkward to clear later (the pin editor disables itself while the master switch is off).
- **Another browser is the active view there** — its shared settings are only accepted from its active browser, so the settings half is refused. The pins are still written: unlike settings, a broker's mod policy can be changed while somebody else is using it.
- **Unreachable**, or **refused our password**.

## Adopt

**Adopt from a broker…** makes this browser match a broker you pick. It reads what that broker hands every browser that loads it — its pins, or the shipped default of each mod where it has no pin — plus its mod settings, shows you the diff, and applies it here.

One limit follows from the same rule as above: a choice some browser over there made **only for itself** is invisible from here. Adopting from a broker that pins nothing therefore gives you its *defaults*, not whatever its users happen to be running. Pushing is what creates pins, so adopting from a broker you have pushed to gives you exactly what you pushed.

A mod that **this** broker pins is not this browser's call, so adopt reports it as refused instead of pretending to change it.

## What travels, and what doesn't

Only settings owned by a mod that is **on here** travel. A mod that is off here is being turned off there too, so its settings don't apply — but note the consequence: a value a disabled mod deliberately keeps (the terminal font mod preserves your font choice so re-enabling restores it) stays behind. If you want such a value to travel, turn the mod on, push, then turn it off again.

Core settings are never touched by either direction — only keys a mod owns. Per-host things like the default profile, start path and default host are deliberately not part of a mod setup.
