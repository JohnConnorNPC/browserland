The update chip tells you whether the brokers you have configured are current with upstream, without opening a shell to run `git fetch`. It checks **every configured host**, not just the serving broker, and each reports its own state. Turn it on or off from Control Panel → Mods.

The mod ships **disabled by default**, and each broker has a second switch of its own governing whether that machine may reach GitHub at all. The mod's toggle decides what your browser draws; the broker's decides whether the machine makes an outbound request. When it is on, the broker makes the request once and caches it for a day, so every tab and every browser shares one call rather than each burning GitHub's rate limit.

**Check now** is what re-asks GitHub before that day is up — the periodic poll never does, so without it a commit pushed in the last 24 hours stays invisible (and an apply has no fresh commit to act on). Because it bypasses the daily cache it also bypasses the rate-limit strategy, so the broker limits it: one re-ask per minute, and six per hour. When it declines, you still get an answer — the one it already had — and the window says so and roughly how long until it will re-ask. That refusal is never dressed up as a fresh result. Two of the reasons are ordinary pacing; the third is GitHub itself rate-limiting that machine, and clicking harder is what extends it.

**Turning the mod on asks the serving broker to open its switch.** Ticking it in Control Panel → Mods is treated as your decision that this machine may check, and the broker applies it immediately — no config file, no restart. Only that click counts: a page reload, a preference synced in from another browser, and a mod pinned on by another operator all leave the broker's switch exactly as it was, so nothing here can make a machine start reaching the internet without somebody choosing it.

If your broker was already checking-off when you upgraded — its mod preference was set before this existed, so no click ever happens — the **Update check** window offers **Enable checking on this broker**. The same row is where you switch it back off, which is the only thing that does: turning the mod off just stops your browser drawing the chip. The grant is stored beside the broker's state file, so it survives restarts.

**Any broker in the list can be switched on, not just the one serving the page.** That is the case that matters when you administer a fleet remotely: the machine that needs switching on is usually one you have no desktop session on. Each row carries its own control, and enabling a remote broker asks you to confirm first — it names the machine and its address, because the rows look alike and the first check discloses that machine's address to GitHub in a way switching it off later does not undo.

A row shows no control when there is nothing to do: when that broker's config owns the setting, when it is too old to have the switch, when it never answered, or when it is already checking because its own config says so. A broker running the build that first shipped this feature is also left alone — it accepts the change only from its own desktop, so the row says to update it rather than offering a button that would fail.

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

## Restarting the broker

This window also carries a **Restart this broker** button (where it is available). Clicking it triggers a *broker-generation restart*: the supervisor process stays alive and re-spawns the broker worker, which picks up any code changes on disk without stopping the machine. It is not a service restart — systemd's launcher preamble and the unit environment are not re-run, and agents you launched before the restart reconnect to the new worker and survive.

Restart is **opt-in** through the broker's config file only: an operator puts `restart_enabled: true` in `broker_config.json`, and there is no GUI switch. The intent is that a restart is a deliberate maintenance choice, not something a browser session earns by being logged in.

Before restarting, the broker **drains** — it stops accepting new work, waits for critical writes (uploads, recording saves) already in flight to finish or time out, and disconnects idle sessions (those not currently running a command) with a warning. Sessions that survive are reported in a confirm dialog before anything stops: **guaranteed** (agents that survive the restart), **at_risk** (sessions that will be lost), and **unknown** (those whose fate couldn't be determined). Every plain terminal on this broker — not an agent session — is lost. The restart waits up to 90 seconds for the new broker to start answering; if it does not, the UI says so and you may need to check the machine itself.

The button is disabled with a reason when:
- **Restart is switched off on this broker** — the config does not have `restart_enabled: true`. This is deployment policy, not a permission a session earns.
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

The **Update…** button in the update window (available only on the serving broker) applies the update that the check has previewed. It is manual-trigger only — no schedule, no auto-install, never automatic. Clicking it shows a confirm dialog with the commit range from the current build to the target, the commit count, a GitHub compare link, and a preview of your session cost — which terminals and agents will survive the restart, and which will be lost.

Applying an update requires **three config gates**, all on the broker that is doing the applying. They are **config-file only**; there is no GUI switch:

- `update_check_enabled: true` — the broker has checked for updates (required to know what commit to apply).
- `update_apply_enabled: true` — the broker is permitted to apply updates.
- `restart_enabled: true` — the broker is permitted to restart itself after applying (a restart without this gate is refused; applying without restarting would leave old code running).

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
- **Apply is not enabled** — the broker's config has not set `update_apply_enabled: true`.
- **Dependency mismatch** — the apply would introduce new or removed dependencies; install or uninstall them manually first.

After an update applies and the broker restarts, the **last deploy** outcome is visible in the next update check — it shows whether the restart succeeded, whether the new build came up, and whether a rollback happened. A build that never comes up is automatically rolled back to the recorded previous commit, visible as a `rolled-back` outcome; the UI shows this so you know why the build changed back rather than forward.

**The broker that was running the old code exits and is replaced with new code.** Agents and their sessions survive the restart and reconnect to the new process, but **terminals and agents do not live-reload**: they keep running the old code they imported at startup. The update window offers a **Fresh terminal** button to spawn a new one with the updated code.

## What this does not claim

The working tree is not inspected: build ids carry no dirty-tree marker, so uncommitted local changes are invisible and the report reflects your last commit only. The comparison is taken from the serving broker; each remote broker's own comparison would be the same repository read hours apart and is not particularly useful.
