The update chip tells you whether the brokers you have configured are current with upstream, without opening a shell to run `git fetch`. It checks **every configured host**, not just the serving broker, and each reports its own state. Turn it on or off from Control Panel → Mods.

The mod ships **disabled by default**, and each broker has a second switch of its own governing whether that machine may reach GitHub at all. The mod's toggle decides what your browser draws; the broker's decides whether the machine makes an outbound request. When it is on, the broker makes the request once and caches it for a day, so every tab and every browser shares one call rather than each burning GitHub's rate limit.

**Check now** is what re-asks GitHub before that day is up — the periodic poll never does, so without it a commit pushed in the last 24 hours stays invisible (and an apply has no fresh commit to act on). Because it bypasses the daily cache it also bypasses the rate-limit strategy, so the broker limits it: one re-ask per minute, and six per hour. When it declines, you still get an answer — the one it already had — and the window says so and roughly how long until it will re-ask. That refusal is never dressed up as a fresh result. Two of the reasons are ordinary pacing; the third is GitHub itself rate-limiting that machine, and clicking harder is what extends it.

**Turning the mod on asks the serving broker to open every switch your click is allowed to open.** Ticking it in Control Panel → Mods is treated as your decision that this machine may check for updates — and, in the same request, that it may apply one and restart itself to run it, for whichever of those two this broker's config has not already claimed. The broker applies the grant immediately — no config file, no restart — but strictly per gate, never a blanket "all three": a gate a config key already owns (`update_check_enabled`, `update_apply_enabled`, or `restart_enabled`, present as either `true` or `false`) is left exactly as that file says, and only the gates still undecided open. Only that one click counts: a page reload, a preference synced in from another browser, and a mod pinned on by another operator all leave every switch exactly as it was, so nothing here can make a machine start reaching the internet, applying an update, or restarting without somebody choosing it.

If your broker was already checking-off when you upgraded — its mod preference was set before this existed, so no click ever happens — the **Update check** window carries that same grant as **two rows**: **Enable checking on this broker**, and **Allow this broker to update itself**, which grants applying and restarting together in one switch (see *Restarting the broker*, below, for why the two travel as a pair). Each row is where you switch its own grant back off; turning the mod off does not touch either one — it only stops your browser drawing the chip. Both grants are stored beside the broker's state file, so they survive restarts.

**Any broker in the list can be switched on, not just the one serving the page** — that is the case that matters when you administer a fleet remotely: the machine that needs switching on is usually one you have no desktop session on. Both rows work this way. Enabling a remote broker's checking asks you to confirm first — it names the machine and its address, because the rows look alike and the first check discloses that machine's address to GitHub in a way switching it off later does not undo. Enabling a remote broker's self-update row asks for its own confirmation, titled **Let that broker update itself?** — it names that machine and its address too, and spells out plainly what the grant is: that machine will download and run code from GitHub and restart itself.

A checking row shows no control when there is nothing to do: when that broker's config owns the setting, when it is too old to have the switch, when it never answered, or when it is already checking because its own config says so. A broker running the build that first shipped this feature is also left alone — it accepts the change only from its own desktop, so the row says to update it rather than offering a button that would fail. The self-update row behaves differently: it stays visible on any broker new enough to report anything at all, and instead says why it cannot be switched — naming the config key that owns whichever gate is blocking it, or, on a broker that predates the row entirely, asking you to update that broker.

An operator who wants a gate taken out of the GUI's hands entirely puts its matching key in that broker's config file: `update_check_enabled` for checking, or `update_apply_enabled` and `restart_enabled` together for the self-update row (the row needs both open to switch on, so pinning either one alone keeps the row shut). Naming a key there — either way, `true` or `false` — makes that file authoritative for that one gate: the row goes dead and names the file, and editing it and restarting stays the way the setting changes. Owning one gate never reaches into another — a config that pins checking `true` does not grant applying, and a config that pins applying `false` does not stop the row from granting checking, or, once its own config allows it, restarting. All three keys are absent from the shipped example configs on purpose, so a normal install leaves every gate the browser's to decide.

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

When a broker did not answer, the window explains why in full sentences. A manual-update note — stop the broker, `git pull --ff-only` in its checkout on its own machine, reinstall dependencies if `pyproject.toml` changed, then start it again and reload the page — appears only for brokers that are behind **and** have no working one-click **Update…** button (see below); it names exactly those brokers. A behind broker whose row carries a live button gets no by-hand instructions, because it doesn't need any.

## Restarting the broker

This window also carries a **Restart this broker** button (where it is available). Clicking it triggers a *broker-generation restart*: the supervisor process stays alive and re-spawns the broker worker, which picks up any code changes on disk without stopping the machine. It is not a service restart — systemd's launcher preamble and the unit environment are not re-run, and agents you launched before the restart reconnect to the new worker and survive.

Restart is **opt-in**, granted from whichever place owns the gate. When the **Allow this broker to update itself** row (above) is writable, turning it on grants restart together with apply — live, no config edit, no process restart. When this broker's config already names `restart_enabled` — `true` or `false` — that file decides instead, and the row goes dead and names it rather than offering a switch. The intent is unchanged either way: a restart is a deliberate choice, made once by whoever owns the gate, not something a browser session earns just by being logged in.

Before restarting, the broker **drains** — it stops accepting new work, waits for critical writes (uploads, recording saves) already in flight to finish or time out, and disconnects idle sessions (those not currently running a command) with a warning. Sessions that survive are reported in a confirm dialog before anything stops: **guaranteed** (agents that survive the restart), **at_risk** (sessions that will be lost), and **unknown** (those whose fate couldn't be determined). Every plain terminal on this broker — not an agent session — is lost. The restart waits up to 90 seconds for the new broker to start answering; if it does not, the UI says so and you may need to check the machine itself.

The button is disabled with a reason when:
- **Restart is switched off on this broker** — when the **Allow this broker to update itself** row is writable, turning it on grants this (together with apply); when this broker's config already names `restart_enabled`, that file decides and the row names it instead. Either way this is not a permission a session earns by being logged in — someone has to grant it, from the row or from the file.
- **This broker was started without the launcher** — it is running `python -m webterm.broker` by hand or in a way the supervisor cannot restart it. Restart it manually on the machine itself.
- **The launcher's parent is no longer running** — the process tree changed (you might have reparented the broker to a different shell). Restart manually.
- **Systemd will not restart this unit** — the unit's `Restart=` policy is `no`, `on-success`, `on-abort`, `on-abnormal`, or `on-watchdog`, none of which respawn on a plain non-zero exit. A graceful stop now would leave nothing listening. Restart manually or change the unit.
- **A restart is already under way** — wait for it to finish and check back.
- **The broker started moments ago** — a cooldown (`restart_cooldown_seconds`, default 90, config-file only, `0` disables) refuses back-to-back restarts so a run of deliberate clicks cannot exhaust the supervisor's crash budget. It clears by itself, and the message says roughly how long is left.
- **This broker could not read its restart policy** — the broker could not contact systemd or parse the unit. Restart manually or give the broker time to try again.

The session cost is shown in the confirm dialog itself, a moment before anything stops.

After a restart, a new `bootId` is reported by the broker (`GET /info` returns `restart.bootId`). The window watches it for you: only a *changed* `bootId` is treated as proof the restart happened. If it never changes, the restart did not happen — the broker either refused it or failed to relaunch, and the window says so.

## Windows scheduled-task note

When the broker runs under a Windows scheduled task (instead of systemd), the **Stop** task gives the broker no graceful shutdown window — it terminates the process immediately. Draining has no time to run, and sessions are disconnected without warning. If you use a scheduled task and need orderly restarts, stop the task, wait a moment for the broker to exit, then restart it by hand or let the scheduler do it on schedule. This is honest rather than hidden: the scheduled-task path stops brokers abruptly whether you restart or not, so the restart machinery has no grace period to exploit.

## Applying an update

The **Update…** button applies the update that the check has previewed. It is manual-trigger only — no schedule, no auto-install, never automatic. Clicking it shows a confirm dialog with the commit range from the current build to the target, the commit count, a GitHub compare link, and a preview of the session cost — which terminals and agents will survive the restart, and which will be lost.

The serving broker's own button sits beside its Restart control. Since the fleet update work (#205), **every behind remote broker's row carries the same button**, driving that broker's own apply: the confirm dialog names the broker it acts on, shows *that broker's* commit range, and reports what *that broker* says a restart would cost its sessions (or says plainly that the session impact is unknown when it hasn't reported one). Each row is busy on its own — applying to one broker never blocks another, and each target refuses a second apply while one is already under way. The button degrades honestly instead of failing noisily: a broker whose own gates are off gets a dead button whose words point at its consent row (or at its config when a config key owns the gate); a broker too old to carry the three-gate consent view — or one that hasn't reported support for applies driven from another broker's page — gets no button and a note saying to update that broker on its own machine.

Applying an update requires **three gates**, all on the broker that is doing the applying — checking, applying, and restarting. Each is owned by whichever decided it first: a present config key (`update_check_enabled` / `update_apply_enabled` / `restart_enabled`) locks its own gate, `true` or `false`, and only editing that file and restarting changes it; an absent key leaves the gate to this window instead — the **Enable checking on this broker** row for the first, and **Allow this broker to update itself** for the other two together (see above). Nothing here silently escalates: a broker that only ever had checking granted — by an old click, an old sidecar record, or a config that names only `update_check_enabled` — stays exactly that, forever, until the self-update row or its own config grants the other two as well.

- `update_check_enabled: true` — the broker has checked for updates (required to know what commit to apply). Granted by the mod's own on-click consent, or the **Enable checking on this broker** row, whenever the config doesn't already own it.
- `update_apply_enabled: true` — the broker is permitted to apply updates. Granted together with the restart gate by **Allow this broker to update itself**, whenever the config doesn't already own it.
- `restart_enabled: true` — the broker is permitted to restart itself after applying (a restart without this gate is refused; applying without restarting would leave old code running). Granted by the same row, together with apply.

**Stop** on the self-update row only ever gives back what that row granted — applying and restarting — never checking: turning self-update off leaves the broker still checking for updates, so the window keeps reporting what's available even after it can no longer act on that by itself.

If all three are enabled and the current state allows applying, clicking **Update…** starts the apply:

1. The broker **fetches from the pinned upstream** (a git subprocess to GitHub with the explicit HTTPS URL and explicit ref, never a remote name).
2. It **verifies** the target commit is now present and that a fast-forward is safe (no local work would be lost).
3. It **checks dependencies**: if the apply introduces a dependency change (e.g. `pyproject.toml` was edited), the apply is refused with details so you can decide whether to run the installer first.
4. It **applies** the commit via git fast-forward with repository hooks disabled, then re-spawns the broker process.

An update may be refused for any of these reasons:

- **Not a git checkout** — this is a pip/wheel install, not a source checkout (out of scope for apply).
- **Dirty working tree** — staged or unstaged local modifications exist; commit, stash, or revert them first.
- **Local commits ahead of upstream** — the checkout carries commits upstream doesn't have; this is a human decision, so the apply refuses.
- **No established update check** — run a check that succeeds first, so the apply knows which commit it is applying.
- **Already current** — the checkout is already at the upstream head.
- **Restart is not available** — the broker cannot restart itself (the button shows why).
- **Apply is not enabled** — this broker's apply gate is off, either because its config has not set `update_apply_enabled: true` or because nobody has granted it yet from the **Allow this broker to update itself** row.
- **Dependency mismatch** — the apply would introduce new or removed dependencies; install or uninstall them manually first.

After an update applies and the broker restarts, the **last deploy** outcome is visible in the next update check — it shows whether the restart succeeded, whether the new build came up, and whether a rollback happened. A build that never comes up is automatically rolled back to the recorded previous commit, visible as a `rolled-back` outcome; the UI shows this so you know why the build changed back rather than forward.

**The broker that was running the old code exits and is replaced with new code.** Agents and their sessions survive the restart and reconnect to the new process, but **terminals and agents do not live-reload**: they keep running the old code they imported at startup. The update window offers a **Fresh terminal** button to spawn a new one with the updated code.

## What this does not claim

The working tree is not inspected: build ids carry no dirty-tree marker, so uncommitted local changes are invisible and the report reflects your last commit only. The comparison is taken from the serving broker; each remote broker's own comparison would be the same repository read hours apart and is not particularly useful.
