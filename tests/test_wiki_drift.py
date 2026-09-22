"""Drift guards: fail the suite when the wiki and the code disagree.

``wiki/`` is the single source for both the GitHub Wiki and the in-app Help
window, which means a wrong sentence there is wrong in two places at once. The
regenerate-and-diff guard in ``test_help_corpus.py`` already pins the corpus to
the wiki's bytes; these guards pin the wiki's *claims* to the code.

Four of them:

* **The default-keybinding table.** ``wiki/Keyboard-Shortcuts.md`` wraps it in
  ``help:ignore`` precisely BECAUSE Help injects the user's live bindings
  instead -- so the corpus never sees it and nothing else checks it. Parse the
  markdown table and compare it against ``DEFAULT_KEYBINDINGS`` plus the
  mod-registered actions.
* **No dangling ``docs/``.** That tree was merged into ``wiki/``; a
  reintroduced ``docs/*.md`` link points at nothing.
* **Mod help coverage.** Every shipped mod dir carries a ``help.md``, so a new
  feature arrives with documentation instead of a year later.
* **The MCP scopes contract** (#236). Every scope name a client or operator has
  to get right is findable in the Technical Reference, and no page still calls
  the per-window MCP mode ephemeral.
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
    only core would miss seven rows. #213 moved 'toggle-help' the same way --
    the action is contributed by the help mod now, backed by the 'help:toggle'
    command, so it comes and goes with the mod that implements it.
    """
    labels: dict[str, str] = {}
    for path in (BROKER / "78_js_keybindings.js",
                 MODS / "workspaces" / "workspaces.js",
                 MODS / "help" / "help.js"):
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


# --------------------------------------------------------------------------- #
# 4. the MCP scopes contract (#236, umbrella #237)
# --------------------------------------------------------------------------- #

def _tr_paragraph(tr: str, head: str) -> str:
    """The Technical Reference paragraph that opens with ``head``, up to the
    next bold route heading or section."""
    start = tr.index(head)
    ends = [i for i in (tr.find("\n\n**`", start + 1), tr.find("\n### ", start))
            if i != -1]
    assert ends, f"no paragraph end after {head!r} in the Technical Reference"
    return tr[start:min(ends)]


def test_the_mcp_scope_contract_is_named_in_the_technical_reference():
    """Every scope name a client or operator has to get right is findable in
    the Technical Reference, each in the paragraph that owns it: the header
    and its grammar (webterm.protocol's own), bad_scope, the /info
    capability, the /mcp/terminals and /mcp/info fields, /session/mcp's
    `mode: null`, /mcp/launch's `mode`, and the per-window sidecar's keys and
    path override. The keys a LIVE /mcp/config answers are checked against a
    running broker in tests/test_mcp_scope.py."""
    from webterm.protocol import SCOPE_HEADER, SCOPE_RE
    tr = (WIKI / "Technical-Reference.md").read_text(encoding="utf-8")
    scopes = tr[tr.index("### Scopes (`X-Browserland-Scope`)"):]
    scopes = scopes[:scopes.index("\n### ", 1)]
    assert f"`{SCOPE_HEADER}: <name>`" in scopes
    assert f"`{SCOPE_RE.pattern}`" in scopes
    assert "**400 `bad_scope`**" in scopes
    assert '`"mcp_scopes": true`' in scopes
    assert "**404 `unknown_or_off`**" in scopes
    terminals = _tr_paragraph(tr, "**`GET /mcp/terminals`**")
    assert "`scope` the window's scope tag (`null` when untagged)" in terminals
    info = _tr_paragraph(tr, "**`GET /mcp/info`**")
    assert '"scope":null' in info
    assert "`scope` is the caller's own declared scope echoed back" in info
    session = _tr_paragraph(tr, "**`POST /session/mcp`**")
    assert '"mode": "off"|"read"|"readwrite"|null' in session
    assert '`"mode": null` clears the override' in session
    launch = _tr_paragraph(tr, "**`POST /mcp/launch`**")
    assert '"mode": "off"|"read"|"readwrite"' in launch
    sidecar = _tr_paragraph(tr, "**Sidecar `webterm_mcp_windows.json`**")
    assert "`mcp_windows_path`" in sidecar
    schema = sidecar[sidecar.index("```json"):sidecar.index("```", sidecar.index("```json") + 7)]
    for key in ("scope", "mode", "pid", "host", "seen"):
        assert f'"{key}":' in schema, f"the sidecar schema must show {key!r}"


def test_the_wiki_no_longer_calls_the_mcp_mode_ephemeral():
    """#236 acceptance: the per-window MCP mode is durable now (#229), so no
    page may still say it is not persisted or lives in memory only."""
    stale = re.compile(r"not persisted|in-memory only", re.I)
    hits = [f"{page.name}:{n}" for page in sorted(WIKI.glob("*.md"))
            for n, line in enumerate(
                page.read_text(encoding="utf-8").splitlines(), 1)
            if stale.search(line)]
    assert sorted(WIKI.glob("*.md")), "no wiki pages found: unmeasured"
    assert hits == []


def test_the_user_pages_explain_scopes():
    """#236: the MCP page carries the Scopes section and the Copy .mcp.json
    help (its secret and git warning included), the window menu page names
    the MCP scope rows, and the taskbar page names the badge."""
    mcp = (WIKI / "MCP-and-AI-Agents.md").read_text(encoding="utf-8")
    assert "\n## Scopes\n" in mcp and "\n### Copy .mcp.json\n" in mcp
    section = mcp[mcp.index("\n## Scopes\n"):mcp.index("\n## Tools overview\n")]
    for words in ("admin view", "by design", "7 days", "convention, not security",
                  "Profiles are global", "`scope_unsupported`",
                  "An empty header value counts as no scope",
                  "keep `.mcp.json` out of git", "`PYTHONPATH`",
                  "only to someone already signed in",
                  "only Escape discards it", "dropped without a notice"):
        assert words in section, f"the Scopes section must say {words!r}"
    menus = (WIKI / "Context-Menus.md").read_text(encoding="utf-8")
    assert "**MCP scope**" in menus and "**Set scope...**" in menus
    taskbar = (WIKI / "Taskbar.md").read_text(encoding="utf-8")
    assert "### MCP scope badge" in taskbar


def test_every_default_mode_claim_names_the_scoped_launch():
    """#236 (and #231's owner item): "new windows start in default mode" is
    not true of a window a scoped MCP client launches (it starts in
    `readwrite` unless the launch passes a `mode`), and a mode set on a window
    is stored and survives broker restarts. Every such claim on the MCP page
    and in the root README, read a paragraph or bullet at a time (the README
    wraps), names both. The status entry that only
    lists what the Help window reports is not a claim about new windows."""
    claim = re.compile(r"new (windows|terminals) start|for new windows", re.I)
    found = []
    for rel in ("wiki/MCP-and-AI-Agents.md", "README.md"):
        text = (REPO / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        for chunk in re.split(r"\n\s*\n|\n(?=\s*[-*|] )", text):
            flat = " ".join(chunk.split())
            if claim.search(flat) and "MCP status (this host)" not in flat:
                found.append((rel, flat))
    assert len(found) >= 4, f"expected the known claims, found {len(found)}: unmeasured"
    missing = [f"{rel}: {flat[:90]}" for rel, flat in found
               if "scoped" not in flat or "restart" not in flat]
    assert missing == []


def test_tr_paragraph_says_when_a_heading_has_no_end():
    """The paragraph slicer, both ways: a heading followed by another bold
    route heading yields its own paragraph, and a heading with nothing after
    it fails with a message naming it, not a bare ValueError from min([])."""
    tr = "intro\n\n**`GET /a`** body\n\n**`GET /b`** tail"
    assert _tr_paragraph(tr, "**`GET /a`**") == "**`GET /a`** body"
    with pytest.raises(AssertionError, match="GET /b"):
        _tr_paragraph(tr, "**`GET /b`**")
