The update chip tells you whether the brokers you have configured are current with upstream, without opening a shell to run `git fetch`. It checks **every configured host**, not just the serving broker, and each reports its own state. Turn it on or off from Control Panel → Mods.

The mod ships **disabled by default**, and each broker has a second switch of its own governing whether that machine may reach GitHub at all. The mod's toggle decides what your browser draws; the broker's decides whether the machine makes an outbound request. When it is on, the broker makes the request once and caches it for a day, so every tab and every browser shares one call rather than each burning GitHub's rate limit.

**Turning the mod on asks the serving broker to open its switch.** Ticking it in Control Panel → Mods is treated as your decision that this machine may check, and the broker applies it immediately — no config file, no restart. Only that click counts: a page reload, a preference synced in from another browser, and a mod pinned on by another operator all leave the broker's switch exactly as it was, so nothing here can make a machine start reaching the internet without somebody choosing it.

If your broker was already checking-off when you upgraded — its mod preference was set before this existed, so no click ever happens — the **Update check** window offers **Enable checking on this broker**. The same row is where you switch it back off, which is the only thing that does: turning the mod off just stops your browser drawing the chip. The grant is stored beside the broker's state file, so it survives restarts.

Only the broker serving the page can be switched on from here. Every other broker in the list is read-only about its own switch, and reports "checking not enabled there" until somebody turns it on from that machine's own desktop.

An operator who wants the decision taken out of the GUI's hands entirely puts `update_check_enabled` in that broker's config file. Naming it there — either way, `true` or `false` — makes the config authoritative, the button goes dead and says so, and editing the file and restarting stays the way that setting changes. It is absent from the shipped example configs on purpose, so a normal install is the browser's to decide.

## The aggregate chip

The taskbar chip is one **aggregate** showing the worst state across all configured brokers, with a tooltip listing every broker and its own state. With a single broker configured it looks exactly as it did before. The chip reads **"up to date" only when every configured broker actually answered and every one is current.** A broker that could not be checked never counts as current, so the phrase "up to date" means the whole fleet is known to be clean.

The chip has three states. **Up to date** means every broker's commit matches upstream. **N behind** (or **N ahead**, or **N unchecked**) means some brokers have that issue — the count is shown because different brokers can be in different states at the same time. **Version ?** means at least one broker could not be checked, and the window explains why for each one.

By default the chip hides itself while the build is current across every broker, so it only takes up taskbar space when there is something to say; an unknown never hides.

A broker you have **hidden** still counts here, unlike on the host status badge: it still colors the chip, still counts toward "N behind" / "N ahead" / "N unchecked", and its line in the tooltip is suffixed `— hidden` rather than dropped. That is the opposite of the host badge's rule (a broker you hide yourself is excluded from that badge entirely — see [[Hosts-and-Multi-Browser]]), and the divergence is deliberate: hiding a broker parks it on the desktop, it does not make that broker's build any less stale, so this chip may stop *claiming* a fault for a hidden broker but must never stop *reporting* one.

## Why a broker might not answer

A peer that cannot answer is distinguished by **why**, and these are five genuinely different things that used to collapse into one "unknown":

- **too old to check** — that broker predates the update route, so it is deliberately not asked (the request would fail its cross-origin preflight and arrive here as an unexplained network error, indistinguishable from a machine that is asleep)
- **checking not enabled there** — it has the route but nobody has switched checking on there (the broker answers 503 to tell us so); this is a choice, not a failure, and it is switched on from that broker's own desktop
- **password refused** — it did not accept our saved password
- **did not answer** — it could not be reached
- **no answer — asleep, or too old to check** — a **headless** broker (one that serves no desktop page) that publishes no update capability. An ordinary broker's own reported version is enough to prove it predates the route, but a headless peer answers with an empty mod list regardless of what routes it actually has, so there is nothing here to prove it either way — it is either too old to have the check, or new enough to have it and simply asleep. Both die in the same cross-origin preflight and come back as the same opaque network error, so rather than guess, the chip and the window say both are possible

Plus the existing failure reasons for when a check *does* run and fails: **offline** (GitHub unreachable), **rate-limited**, **no git checkout**, or the comparison **unavailable** (committed but never pushed, garbage-collected, or the branch force-pushed).

## The detail window

Click the chip to open the **Update check** window. It shows every configured broker as one row, its own state in words, and its build and commit. The upstream build is shown once (it is the same constant for every broker). The window shows when the checks last ran — the oldest of the batch, because a broker that answered an hour ago is not as fresh as one that just answered.

When a broker did not answer, the window explains why in full sentences. When you are behind it spells out the manual update: stop the broker, `git pull --ff-only`, reinstall dependencies if `pyproject.toml` changed, then start it again and reload the page.

## What this does not claim

The working tree is not inspected: build ids carry no dirty-tree marker, so uncommitted local changes are invisible and the report reflects your last commit only. The comparison is taken from the serving broker; each remote broker's own comparison would be the same repository read hours apart and is not particularly useful.
