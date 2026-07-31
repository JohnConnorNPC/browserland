"""Drift guards: fail the suite when the wiki and the code disagree.

``wiki/`` is the single source for both the GitHub Wiki and the in-app Help
window, which means a wrong sentence there is wrong in two places at once. The
regenerate-and-diff guard in ``test_help_corpus.py`` already pins the corpus to
the wiki's bytes; these guards pin the wiki's *claims* to the code.

Three of them:

* **The default-keybinding table.** ``wiki/Keyboard-Shortcuts.md`` wraps it in
  ``help:ignore`` precisely BECAUSE Help injects the user's live bindings
  instead -- so the corpus never sees it and nothing else checks it. Parse the
  markdown table and compare it against ``DEFAULT_KEYBINDINGS`` plus the
  mod-registered actions.
* **No dangling ``docs/``.** That tree was merged into ``wiki/``; a
  reintroduced ``docs/*.md`` link points at nothing.
* **Mod help coverage.** Every shipped mod dir carries a ``help.md``, so a new
  feature arrives with documentation instead of a year later.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WIKI = REPO / "wiki"
BROKER = REPO / "webterm" / "broker"
MODS = BROKER / "mods"


# --------------------------------------------------------------------------- #
# 1. the default-keybinding table vs. the real defaults
# --------------------------------------------------------------------------- #

def _js_default_keybindings() -> dict[str, str]:
    """`id -> combo` from DEFAULT_KEYBINDINGS in 54_js_app_windows_store.js."""
    src = (BROKER / "54_js_app_windows_store.js").read_text(encoding="utf-8")
    m = re.search(r"const DEFAULT_KEYBINDINGS = \{(.*?)\n        \};", src, re.S)
    assert m, "DEFAULT_KEYBINDINGS not found -- did it move or get renamed?"
    return dict(re.findall(r"'([a-z0-9-]+)':\s*'([^']+)'", m.group(1)))


def _js_action_labels() -> dict[str, str]:
    """`id -> human label` for every bindable action, core AND mod-registered.

    Core actions live in 78_js_keybindings.js; the workspace ones are
    registered by the workspaces mod, which is exactly why a guard that read
    only core would miss seven rows.
    """
    labels: dict[str, str] = {}
    for path in (BROKER / "78_js_keybindings.js",
                 MODS / "workspaces" / "workspaces.js"):
        src = path.read_text(encoding="utf-8")
        for aid, label in re.findall(
                r"\{\s*id:\s*'([a-z0-9-]+)',\s*label:\s*'([^']+)'", src):
            labels.setdefault(aid, label)
    return labels


def _wiki_keybinding_rows() -> list[tuple[str, str]]:
    """`(action label, combo-or-empty)` from the Default bindings table."""
    text = (WIKI / "Keyboard-Shortcuts.md").read_text(encoding="utf-8")
    m = re.search(r"^## Default bindings$(.*?)(?=^<!-- help:ignore-end -->)",
                  text, re.S | re.M)
    assert m, "the 'Default bindings' table is gone from Keyboard-Shortcuts.md"
    rows = []
    for line in m.group(1).split("\n"):
        line = line.strip()
        if not line.startswith("|") or re.match(r"^\|[\s:|-]+\|$", line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 2 or cells[0] == "Action label":
            continue
        combo = cells[1]
        # `Ctrl+Alt+p` -> Ctrl+Alt+p ; *(unbound by default)* -> ""
        cm = re.fullmatch(r"`([^`]+)`", combo)
        rows.append((cells[0], cm.group(1) if cm else ""))
    return rows


def test_wiki_keybinding_table_matches_the_real_defaults():
    defaults = _js_default_keybindings()
    labels = _js_action_labels()
    by_label = {labels[aid]: combo for aid, combo in defaults.items()
                if aid in labels}
    unbound = {labels[aid] for aid in labels if aid not in defaults}

    rows = _wiki_keybinding_rows()
    assert rows, "parsed no rows out of the Default bindings table"

    documented = {label for label, _ in rows}
    # Every bindable action is in the table...
    missing = (set(by_label) | unbound) - documented
    assert not missing, (
        "wiki/Keyboard-Shortcuts.md's Default bindings table is missing: %s"
        % sorted(missing))
    # ...and the table invents none.
    extra = documented - (set(by_label) | unbound)
    assert not extra, (
        "wiki/Keyboard-Shortcuts.md documents actions that do not exist: %s"
        % sorted(extra))
    # ...with the right combo, including the deliberately-unbound one.
    for label, combo in rows:
        if label in unbound:
            assert combo == "", (
                "%r has no default binding in the code, but the wiki gives %r"
                % (label, combo))
        else:
            assert combo == by_label[label], (
                "%r: wiki says %r, DEFAULT_KEYBINDINGS says %r"
                % (label, combo, by_label[label]))


# --------------------------------------------------------------------------- #
# 2. no dangling docs/
# --------------------------------------------------------------------------- #

_DOCS_REF = re.compile(r"docs/[A-Za-z_]+\.md")
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".pytest_cache",
              "build", "dist", ".claude", ".playwright-mcp"}
_TEXT_SUFFIXES = {".md", ".py", ".js", ".css", ".html", ".sh", ".ps1", ".bat",
                  ".yml", ".yaml", ".toml", ".txt", ".json"}


def _tracked_files() -> list[Path]:
    """Git-tracked files only.

    Deliberately not a filesystem walk: an untracked scratch file (a plan, a
    scraped page, a note-to-self) may legitimately quote the old paths, and
    failing the suite over something git does not carry would train people to
    ignore this guard. Falls back to a walk outside a git checkout.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            return [REPO / n for n in out.stdout.split("\0") if n]
    except (OSError, subprocess.SubprocessError):
        pass
    return [p for p in REPO.rglob("*") if p.is_file()]


def test_no_tracked_file_references_the_retired_docs_tree():
    # docs/ was merged into wiki/. A reintroduced docs/*.md link points at
    # nothing -- on GitHub, in a checkout, and in the published Wiki alike.
    offenders = []
    for path in _tracked_files():
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        # This guard names the pattern it forbids; it cannot police itself.
        if path.name == "test_wiki_drift.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for m in _DOCS_REF.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append("%s:%d: %s"
                             % (path.relative_to(REPO).as_posix(), line, m.group(0)))
    assert not offenders, (
        "docs/ was merged into wiki/; these references point at nothing:\n  "
        + "\n  ".join(offenders))


# --------------------------------------------------------------------------- #
# 3. mod help coverage
# --------------------------------------------------------------------------- #

# wiki/Workspaces.md owns that slug (pinned in test_ui_assets.py), so the
# workspaces mod deliberately ships no help.md of its own -- a second section
# under the same slug would be a duplicate-slug BuildError.
_NO_HELP_ALLOWED = {"workspaces"}


def test_every_shipped_mod_carries_a_help_page():
    missing = sorted(d.name for d in MODS.iterdir()
                     if d.is_dir()
                     and (d / "mod.json").is_file()
                     and not (d / "help.md").is_file()
                     and d.name not in _NO_HELP_ALLOWED)
    assert not missing, (
        "these shipped mods have no help.md, so their features are undocumented "
        "in BOTH the wiki and the in-app Help: %s. Add mods/<id>/help.md and "
        "regenerate with `python -m webterm.broker.help_corpus`." % missing)


@pytest.mark.parametrize("allowed", sorted(_NO_HELP_ALLOWED))
def test_the_help_allowlist_stays_justified(allowed):
    # If an allowlisted mod ever grows a help.md, the entry is dead and should
    # go -- otherwise the allowlist quietly becomes a place to hide.
    mod = MODS / allowed
    assert mod.is_dir(), (
        "%r is allowlisted as needing no help.md but the mod is gone; drop the "
        "entry from _NO_HELP_ALLOWED" % allowed)
    assert not (mod / "help.md").is_file(), (
        "%r now ships a help.md, so its _NO_HELP_ALLOWED entry is obsolete" % allowed)
