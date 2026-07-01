The Git status widget puts a **⎇** button and the current branch name on the title bar of every terminal whose working directory is a git repository, so you can see a shell's branch and whether it is dirty without running `git status` yourself. It appears whenever the Git status mod is enabled — there is no separate toggle. Turn it on or off from Control Panel → Mods.

The mod ships **disabled by default**. Enabling it decorates every open terminal (and every one you open afterwards) and starts a slow background poll — each check runs a short `git` command in that terminal's live working directory through the broker, so it stays off until you opt in.

## The button and branch label

When the terminal's directory is a repository, the ⎇ button lights up and the branch name is shown next to it (or `detached` on a detached HEAD). The label turns amber and gains a **●** dirty badge — with a change count when one is known — whenever there are uncommitted changes.

The button is **muted** (dimmed, no label) when the directory is not a git repository, and it is **hidden** entirely on an older broker that does not provide the git endpoint.

## The status popover

Click the ⎇ button to open a small popover anchored under it. For a repository it lists the branch (or `detached HEAD`), how far you are **ahead** and **behind** the upstream, and the counts of **staged**, **unstaged**, **untracked**, and **conflicts** entries (conflicts are highlighted), plus whether the tree is clean or dirty. A **Refresh** button re-checks on demand. Click the button again, click outside the popover, or press `Escape` to close it.

The widget refreshes on its own every 15 seconds while a terminal is open, and once more each time you open the popover.
