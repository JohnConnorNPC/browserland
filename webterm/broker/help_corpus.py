"""Parse the end-user wiki (``wiki/*.md``) into the in-app Help corpus.

The in-app Help guide (the taskbar "?" chip) used to carry a hand-written
``HELP_ENTRIES_STATIC`` array inside the desktop UI page that duplicated the
``wiki/`` pages — two copies that drifted apart (see issue #57). This module
makes ``wiki/*.md`` the SINGLE SOURCE OF TRUTH: it parses the markdown into a
typed, plain-data corpus that the broker serves at ``GET /help-corpus.json``
and the frontend renders with DOM APIs only (createElement + textContent).

The corpus is deliberately *not* HTML. Each card body is a list of typed
BLOCKS, each block a list of typed inline SPANS::

    corpus  = { "sections": [ section, ... ] }
    section = { "slug": str,            # stable id (lowercased file stem)
                "label": str,           # human display name (from _Sidebar.md)
                "order": int,           # sidebar order
                "cards": [ card, ... ] }
    card    = { "title": str,
                "body": [ block, ... ],
                "search": str }         # precomputed lowercased plain text
    block   = { "t": "p"|"bullet"|"sub", "spans": [ span, ... ] }
    span    = { "t": "text"|"strong"|"code"|"kbd", "v": str }

The frontend renders only this fixed, known set of types; anything the parser
cannot classify degrades to a plain ``text`` span — NEVER to raw HTML. This
keeps the long-standing XSS-safety invariant of the Help renderer intact even
though the content now comes from files.

``build_corpus`` is strict: it RAISES on structural problems (unbalanced
``help:ignore`` markers, duplicate slugs) so tests / the regeneration step
catch them. ``load_corpus`` (used at import) is protective: it parses the live
wiki when present, else falls back to the packaged ``help_corpus.json``, else
an empty corpus — so a missing/broken wiki degrades Help gracefully and never
breaks broker startup.
"""

from __future__ import annotations

import html
import json
import logging
import re
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Repo layout: this file is webterm/broker/help_corpus.py, so parents[2] is the
# repo root (parents[0]=broker, [1]=webterm, [2]=repo) — NOT the process cwd,
# which the broker cannot rely on.
_REPO_ROOT = Path(__file__).resolve().parents[2]
WIKI_DIR = _REPO_ROOT / "wiki"
# Packaged fallback, shipped next to this module (see pyproject package-data),
# so installed runs that don't carry wiki/ still have Help content.
PACKAGED_JSON = Path(__file__).resolve().parent / "help_corpus.json"
# In-repo frontend mods (webterm/broker/mods/<id>/) — the same dir ui.py splices
# scripts from — may each drop a wiki-format help.md that becomes a Help section
# (issue #113). Next to this module (broker/), NOT under the repo root.
MODS_DIR = Path(__file__).resolve().parent / "mods"
# Mod sections sort AFTER every wiki section (sidebar orders are small); a mod
# may override with an explicit help.order in its mod.json.
_MOD_ORDER_BASE = 2000

# Pages that are navigation / boilerplate, never cards.
_EXCLUDE_STEMS = {"home", "_sidebar", "_footer"}

# A final card with one of these exact (case-insensitive) titles is cross-nav
# and is dropped by rule (per-page link footers). Anything else excluded must
# use explicit <!-- help:ignore-start/end --> markers — never a heuristic.
_CROSSNAV_TITLES = {"related pages", "related", "see also"}

_IGNORE_START = "<!-- help:ignore-start -->"
_IGNORE_END = "<!-- help:ignore-end -->"

# Strict keyboard-combo grammar for turning `code` spans into kbd chips, so
# ordinary code (`/state`, `broker.py`, `mcp/input`, `0`) is NOT mis-rendered
# as a keyboard key.
_MODIFIERS = {"ctrl", "control", "alt", "option", "shift",
              "cmd", "command", "meta", "super", "win"}
_NAMED_KEYS = {
    "escape", "esc", "enter", "return", "tab", "space", "spacebar",
    "delete", "del", "backspace", "insert", "home", "end",
    "pageup", "pagedown", "up", "down", "left", "right",
    "arrowup", "arrowdown", "arrowleft", "arrowright",
}


class BuildError(ValueError):
    """Raised by build_corpus on a structural problem in the wiki source."""


# --------------------------------------------------------------------------- #
# Inline parsing: a markdown line/segment -> a list of typed spans.
# --------------------------------------------------------------------------- #

_WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])[*_]([^*_\n]+)[*_](?![*\w])")
_CODE = re.compile(r"`([^`]+)`")


def _looks_like_combo(text: str) -> bool:
    """True if `text` is a keyboard shortcut (Ctrl+Alt+p, Escape, Ctrl+Alt+1)."""
    text = text.strip()
    if not text:
        return False
    # Comma-separated chord sequence (e.g. "g, g"): every chord must qualify.
    chords = [c for c in re.split(r"\s*,\s*", text) if c != ""]
    if not chords:
        return False
    return all(_is_single_combo(c) for c in chords)


def _is_single_combo(chord: str) -> bool:
    toks = chord.split("+")
    if len(toks) == 1:
        # A lone token is a chip only if it is a well-known named key
        # (Escape/Enter/...). A bare letter/number/path is not.
        return toks[0].strip().lower() in _NAMED_KEYS
    *mods, key = (t.strip() for t in toks)
    if not mods or any(not m for m in (mods + [key])):
        return False
    if not all(m.lower() in _MODIFIERS for m in mods):
        return False
    return _is_key_token(key)


def _is_key_token(key: str) -> bool:
    k = key.strip()
    if not k:
        return False
    if k.lower() in _NAMED_KEYS:
        return True
    if len(k) == 1 and (k.isalnum()):
        return True
    return bool(re.fullmatch(r"[Ff]\d{1,2}", k))


def _code_span(content: str) -> dict:
    content = html.unescape(content)
    return {"t": "kbd" if _looks_like_combo(content) else "code", "v": content}


def _slug_words(slug: str) -> str:
    """Search-helper text from a link target (e.g. 'Window-Modes' -> 'window modes')."""
    return re.sub(r"[-_]+", " ", slug).strip().lower()


def parse_inline(text: str, search_extra: list[str] | None = None) -> list[dict]:
    """Parse one line of inline markdown into typed spans.

    Supported subset: `code` (combo -> kbd), **bold**, *italic*/_em_ (-> text),
    [[Label|Page]] / [[Page]] (-> label text only), [label](url) (-> label),
    HTML entities (decoded). Everything else degrades to plain text. When a
    wiki link is seen, the target's words are appended to ``search_extra`` so
    search still matches the page name even though only the label is rendered.
    """
    spans: list[dict] = []
    # Split on code spans first: their content is literal (no nested parsing).
    parts = _CODE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 1:
            spans.append(_code_span(part))
        else:
            spans.extend(_parse_non_code(part, search_extra))
    return _coalesce(spans)


def _parse_non_code(text: str, search_extra: list[str] | None) -> list[dict]:
    spans: list[dict] = []
    pos = 0
    n = len(text)
    while pos < n:
        nxt = None  # (start, end, span, kind)
        for rx, kind in ((_WIKI_LINK, "wiki"), (_MD_LINK, "md"),
                         (_BOLD, "bold"), (_ITALIC, "italic")):
            m = rx.search(text, pos)
            if m and (nxt is None or m.start() < nxt[0]):
                nxt = (m.start(), m.end(), m, kind)
        if nxt is None:
            spans.append(_text_span(text[pos:]))
            break
        start, end, m, kind = nxt
        if start > pos:
            spans.append(_text_span(text[pos:start]))
        if kind == "wiki":
            inner = m.group(1)
            label, _, target = inner.partition("|")
            shown = (label if target else inner).strip()
            tgt = (target or inner).strip()
            if search_extra is not None and tgt:
                search_extra.append(_slug_words(tgt))
            spans.append(_text_span(shown))
        elif kind == "md":
            spans.append(_text_span(m.group(1)))
        elif kind == "bold":
            spans.append({"t": "strong", "v": html.unescape(m.group(1))})
        else:  # italic -> plain text (degrade)
            spans.append(_text_span(m.group(1)))
        pos = end
    return [s for s in spans if not (s["t"] == "text" and s["v"] == "")]


def _text_span(v: str) -> dict:
    return {"t": "text", "v": html.unescape(v)}


def _coalesce(spans: list[dict]) -> list[dict]:
    """Merge adjacent text spans; drop empties."""
    out: list[dict] = []
    for s in spans:
        if s["v"] == "" and s["t"] == "text":
            continue
        if out and out[-1]["t"] == "text" and s["t"] == "text":
            out[-1]["v"] += s["v"]
        else:
            out.append(dict(s))
    return out


# --------------------------------------------------------------------------- #
# Block parsing: the raw lines of one card -> a list of typed blocks.
# --------------------------------------------------------------------------- #

def _strip_ignored(text: str) -> str:
    """Remove <!-- help:ignore-start --> .. <!-- help:ignore-end --> regions.

    Raises BuildError on unbalanced markers so a typo can't silently erase the
    rest of a page.
    """
    out: list[str] = []
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == _IGNORE_START:
            depth += 1
            continue
        if stripped == _IGNORE_END:
            depth -= 1
            if depth < 0:
                raise BuildError("unbalanced help:ignore (end before start)")
            continue
        if depth == 0:
            out.append(line)
    if depth != 0:
        raise BuildError("unbalanced help:ignore (missing %d end marker(s))" % depth)
    return "\n".join(out)


def _is_table_sep(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(re.fullmatch(r"\|?[\s:|-]+\|?", s)) and "-" in s


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _row_spans(cells: list[str], sep: str, search_extra: list[str]) -> list[dict]:
    spans: list[dict] = []
    for i, cell in enumerate(cells):
        if i:
            spans.append({"t": "text", "v": sep})
        spans.extend(parse_inline(cell, search_extra))
    return _coalesce(spans)


def parse_blocks(text: str, search_extra: list[str]) -> list[dict]:
    """Parse the markdown body of one card into typed blocks."""
    lines = text.split("\n")
    blocks: list[dict] = []
    i = 0
    n = len(lines)
    para: list[str] = []

    def flush_para():
        if para:
            joined = " ".join(p.strip() for p in para).strip()
            if joined:
                blocks.append({"t": "p", "spans": parse_inline(joined, search_extra)})
            para.clear()

    while i < n:
        line = lines[i]
        s = line.strip()
        if s == "":
            flush_para()
            i += 1
            continue
        # Fenced code block.
        if s.startswith("```"):
            flush_para()
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            code = " ".join(l.strip() for l in code_lines).strip()
            if code:
                blocks.append({"t": "p", "spans": [{"t": "code", "v": code}]})
            continue
        # Subheading (### / ####).
        m = re.match(r"^(#{3,6})\s+(.*)$", s)
        if m:
            flush_para()
            blocks.append({"t": "sub", "spans": parse_inline(m.group(2).strip(),
                                                             search_extra)})
            i += 1
            continue
        # Table: a pipe row immediately followed by a separator row.
        if s.startswith("|") and i + 1 < n and _is_table_sep(lines[i + 1]):
            flush_para()
            header = _split_row(line)
            blocks.append({"t": "sub", "spans": _row_spans(header, " · ",
                                                          search_extra)})
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                if _is_table_sep(lines[i]):
                    i += 1
                    continue
                cells = _split_row(lines[i])
                blocks.append({"t": "bullet",
                               "spans": _row_spans(cells, " — ", search_extra)})
                i += 1
            continue
        # List item (-, *, or "N.").
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", s)
        if m:
            flush_para()
            blocks.append({"t": "bullet",
                           "spans": parse_inline(m.group(1).strip(), search_extra)})
            i += 1
            continue
        # Blockquote (possibly multi-line): fold into a "Tip:" paragraph.
        if s.startswith(">"):
            flush_para()
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            qtext = " ".join(q.strip() for q in quote).strip()
            qtext = re.sub(r"^tip:\s*", "", qtext, flags=re.IGNORECASE)
            spans = [{"t": "strong", "v": "Tip: "}]
            spans.extend(parse_inline(qtext, search_extra))
            blocks.append({"t": "p", "spans": _coalesce(spans)})
            continue
        # Plain paragraph text.
        para.append(line)
        i += 1

    flush_para()
    return blocks


# --------------------------------------------------------------------------- #
# Page -> cards, sidebar -> ordering, and the top-level corpus build.
# --------------------------------------------------------------------------- #

def _block_text(block: dict) -> str:
    return " ".join(span.get("v", "") for span in block.get("spans", []))


def _card_search(title: str, blocks: list[dict], extra: list[str]) -> str:
    parts = [title]
    parts.extend(_block_text(b) for b in blocks)
    parts.extend(extra)
    return re.sub(r"\s+", " ", " ".join(parts)).strip().lower()


def parse_page(text: str) -> list[dict]:
    """Parse one wiki page's markdown into a list of cards.

    The intro (text before the first ## heading) becomes an "Overview" card;
    each ## heading becomes a card; a trailing cross-nav card is dropped.
    """
    text = _strip_ignored(text)
    # Split on level-2 headings, keeping the heading title with its body.
    chunks = re.split(r"(?m)^##\s+(.*)$", text)
    cards: list[dict] = []

    intro = chunks[0]
    intro_extra: list[str] = []
    intro_blocks = parse_blocks(intro, intro_extra)
    if intro_blocks:
        cards.append({"title": "Overview", "_blocks": intro_blocks,
                      "_extra": intro_extra})

    for idx in range(1, len(chunks), 2):
        title = chunks[idx].strip()
        body = chunks[idx + 1] if idx + 1 < len(chunks) else ""
        extra: list[str] = []
        blocks = parse_blocks(body, extra)
        cards.append({"title": title, "_blocks": blocks, "_extra": extra})

    # Drop only the final card if it is a cross-nav footer.
    if cards and cards[-1]["title"].strip().lower() in _CROSSNAV_TITLES:
        cards.pop()

    out: list[dict] = []
    for c in cards:
        if not c["_blocks"] and c["title"] == "Overview":
            continue
        out.append({
            "title": c["title"],
            "body": c["_blocks"],
            "search": _card_search(c["title"], c["_blocks"], c["_extra"]),
        })
    return out


def _humanize(stem: str) -> str:
    return re.sub(r"[-_]+", " ", stem).strip()


def parse_sidebar(text: str) -> dict:
    """Map lowercased file stem -> (order, label) from _Sidebar.md link order.

    Bold group headers (**Building layouts**) are ignored for content but their
    position is preserved by document order. [[Home]] (an excluded page) is
    skipped. A page may be listed as [[Page]] (label == slug) or [[Label|Slug]].
    """
    order_map: dict = {}
    n = 0
    for m in _WIKI_LINK.finditer(text):
        inner = m.group(1)
        label, _, target = inner.partition("|")
        slug = (target or inner).strip()
        label = (label if target else inner).strip()
        stem = slug.lower()
        if stem in _EXCLUDE_STEMS:
            continue
        if stem in order_map:
            continue  # first occurrence wins; duplicates don't reorder
        order_map[stem] = (n, label)
        n += 1
    return order_map


def build_corpus(wiki_dir: Path) -> dict:
    """Parse every content page in ``wiki_dir`` into the typed corpus.

    Raises BuildError on a structural problem (unbalanced ignore markers,
    duplicate section slugs).
    """
    wiki_dir = Path(wiki_dir)
    sidebar_path = wiki_dir / "_Sidebar.md"
    sidebar = parse_sidebar(sidebar_path.read_text(encoding="utf-8")) \
        if sidebar_path.is_file() else {}

    pages = sorted((p for p in wiki_dir.glob("*.md")
                    if p.stem.lower() not in _EXCLUDE_STEMS),
                   key=lambda p: p.name.lower())

    sections: list[dict] = []
    seen_slugs: set = set()
    unlisted = 0
    for path in pages:
        stem = path.stem
        key = stem.lower()
        slug = key
        if slug in seen_slugs:
            raise BuildError("duplicate section slug: %s" % slug)
        seen_slugs.add(slug)
        if key in sidebar:
            order, label = sidebar[key]
        else:
            order, label = (1000 + unlisted, _humanize(stem))
            unlisted += 1
        cards = parse_page(path.read_text(encoding="utf-8"))
        if not cards:
            continue
        sections.append({"slug": slug, "label": label, "order": order,
                         "cards": cards})

    sections.sort(key=lambda s: (s["order"], s["slug"]))
    return {"sections": sections}


# --------------------------------------------------------------------------- #
# Mod-owned help: each mods/<id>/help.md is a wiki-format page the SAME parser
# reads, tagged with its owning mod id so the frontend can hide it when the mod
# is disabled (issue #113). No second parser, no markdown on the frontend.
# --------------------------------------------------------------------------- #

def _mod_manifest(mod_dir: Path) -> dict:
    """Best-effort parsed mod.json for one mod dir (mirrors ui.py:_manifest).

    Any read/parse problem, or a non-object payload, yields ``{}`` so a malformed
    manifest can never crash the corpus build (and thus broker import).
    """
    p = Path(mod_dir) / "mod.json"
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - missing / bad JSON / unreadable
        return {}
    return meta if isinstance(meta, dict) else {}


def build_mod_sections(mods_dir: Path = MODS_DIR) -> list[dict]:
    """Parse each mod's optional wiki-format ``help.md`` into a tagged section.

    For every subdir of ``mods_dir`` (sorted, for deterministic fallback order)
    that has BOTH ``mod.json`` and ``help.md``: parse ``help.md`` with the same
    ``parse_page`` the wiki uses; skip it if that yields no cards. The optional
    ``help`` block in mod.json supplies ``slug`` / ``label`` / ``order`` / ``icon``
    with fallbacks: slug = mod id, label = mod.json ``title`` (else humanized id),
    order = ``_MOD_ORDER_BASE + index`` (after the wiki), icon omitted. Every
    section is stamped with its owning mod id (and its icon, when declared) so the
    frontend can hide it while the mod is disabled.

    Strict like ``build_corpus``: raises ``BuildError`` on a duplicate mod slug.
    """
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        return []
    sections: list[dict] = []
    seen_slugs: set = set()
    subdirs = sorted((d for d in mods_dir.iterdir() if d.is_dir()),
                     key=lambda d: d.name.lower())
    for index, mod_dir in enumerate(subdirs):
        if not ((mod_dir / "mod.json").is_file()
                and (mod_dir / "help.md").is_file()):
            continue
        cards = parse_page((mod_dir / "help.md").read_text(encoding="utf-8"))
        if not cards:
            continue
        manifest = _mod_manifest(mod_dir)
        mod_id = str(manifest.get("id") or mod_dir.name)
        block = manifest.get("help")
        if not isinstance(block, dict):
            block = {}
        # `or` folds a missing OR blank manifest value to the fallback (never "").
        slug = str(block.get("slug") or mod_id).strip().lower()
        if slug in seen_slugs:
            raise BuildError("duplicate mod section slug: %s" % slug)
        seen_slugs.add(slug)
        label = str(block.get("label") or manifest.get("title")
                    or _humanize(mod_id))
        order = block.get("order")
        # Reject a non-int (or bool) order so sorting stays total across sections.
        if not isinstance(order, int) or isinstance(order, bool):
            order = _MOD_ORDER_BASE + index
        section = {"slug": slug, "label": label, "order": order,
                   "cards": cards, "mod": mod_id}
        icon = block.get("icon")
        if isinstance(icon, str) and icon:
            section["icon"] = icon
        sections.append(section)
    return sections


def build_full_corpus() -> dict:
    """Merge the wiki corpus with mod-owned help sections into one corpus.

    ``build_corpus(WIKI_DIR)`` (wiki-only, kept intact for parity tests) plus
    ``build_mod_sections()``, re-sorted by ``(order, slug)``. Raises ``BuildError``
    on a slug that collides ACROSS the two sets (a mod can't shadow a wiki page).
    This is THE builder used for serving, regeneration, and the drift test.

    Both dirs are looked up as module globals HERE (not via a frozen default arg)
    so a test can monkeypatch ``WIKI_DIR`` / ``MODS_DIR`` and have it take effect.
    """
    merged = build_corpus(WIKI_DIR)["sections"] + build_mod_sections(MODS_DIR)
    seen: set = set()
    for sec in merged:
        if sec["slug"] in seen:
            raise BuildError("duplicate section slug across wiki+mods: %s"
                             % sec["slug"])
        seen.add(sec["slug"])
    merged.sort(key=lambda s: (s["order"], s["slug"]))
    return {"sections": merged}


# --------------------------------------------------------------------------- #
# Installed-mod help (#163): merged at SERVE time, and ONLY at serve time.
#
# ``build_full_corpus`` above stays SHIPPED-ONLY on purpose. It is what
# ``python -m webterm.broker.help_corpus`` writes into the packaged JSON and
# what tests/test_help_corpus.py byte-matches; folding in whatever happens to be
# installed on the box running the regenerator would make that drift guard
# machine-specific. So installed sections are merged onto ``app.ctx.help_corpus``
# at runtime instead — from the modinstall INDEX, never from a second walk of
# mods_dir. The index captured each mod's help text at install/scan time from
# the very bytes being served, so there is exactly ONE read and no second
# traversal that could disagree with it.
# --------------------------------------------------------------------------- #

# Installed sections sort after the wiki (orders are small) AND after the
# shipped mods (_MOD_ORDER_BASE + n). A mod may still override with help.order.
_INSTALLED_ORDER_BASE = 3000
# modinstall caps every file at 256 KiB, on the install path AND the scan path,
# and a str is never longer than its UTF-8 encoding — so this ceiling is already
# enforced upstream. Restated locally only so a hand-built index still cannot
# hand the parser an unbounded string; it is not a second policy.
_MAX_INSTALLED_HELP_CHARS = 256 * 1024

# help.md text -> parsed cards, rebuilt to EXACTLY the currently-merged set on
# every call. Parsing is a pure function of the text, so a content key can never
# go stale; rebuilding rather than accumulating means the cache holds the same
# card objects the live corpus already holds, so it costs no steady-state
# memory and cannot outgrow the index. It earns its keep because a swap runs ON
# THE EVENT LOOP under mods_install_lock: without it, one install would re-parse
# every other installed mod's help (up to 32 x 256 KiB) before the loop turns.
_installed_cards: dict = {}


def _installed_section(mod_id: str, record: dict, offset: int,
                       cards_out: dict) -> dict | None:
    """One installed mod's Help section, or ``None`` if it has none to give.

    Every field is re-derived defensively from the record: this reads an index
    a hand-populated store could have contributed to, and the caller's contract
    is that nothing in here can blank Help.
    """
    if not isinstance(mod_id, str) or not mod_id:
        return None
    help_md = record.get("help_md") if isinstance(record, dict) else None
    if not isinstance(help_md, str) or not help_md.strip():
        return None
    if len(help_md) > _MAX_INSTALLED_HELP_CHARS:
        LOGGER.warning("installed mod %s: help.md over %d chars, not merged",
                       mod_id, _MAX_INSTALLED_HELP_CHARS)
        return None
    cards = cards_out.get(help_md)
    if cards is None:
        cards = _installed_cards.get(help_md)
    if cards is None:
        cards = parse_page(help_md)
    cards_out[help_md] = cards
    if not cards:
        return None
    manifest = record.get("manifest")
    if not isinstance(manifest, dict):
        manifest = {}
    block = manifest.get("help")
    if not isinstance(block, dict):
        block = {}
    label = block.get("label") or manifest.get("title")
    if not isinstance(label, str) or not label:
        label = _humanize(mod_id)
    order = block.get("order")
    # Reject a non-int (or a bool) so sorting stays total across sections.
    if not isinstance(order, int) or isinstance(order, bool):
        order = _INSTALLED_ORDER_BASE + offset
    # The slug is FORCED to the mod id. modinstall already drops help.slug from
    # the canonical manifest, so this is the second of two places that make a
    # collision with a wiki or shipped slug structurally impossible rather than
    # handled after the fact: an installed id must start with "x-", and no wiki
    # page stem or shipped mod id does.
    section = {"slug": mod_id, "label": label, "order": order,
               "cards": cards, "mod": mod_id}
    icon = block.get("icon")
    if isinstance(icon, str) and icon:
        section["icon"] = icon
    return section


def _section_sort_key(section: dict):
    """A TOTAL order over sections from any source, so the merge sort cannot
    raise on a section carrying an unexpected order/slug type."""
    order = section.get("order")
    if not isinstance(order, int) or isinstance(order, bool):
        order = _INSTALLED_ORDER_BASE
    slug = section.get("slug")
    return (order, slug if isinstance(slug, str) else "")


def merge_installed_sections(corpus: dict, index: dict) -> dict:
    """``corpus`` plus one Help section per installed mod that shipped a help.md.

    Takes the modinstall INDEX, not a directory: the help text was captured at
    install/scan time from the bytes being served, so this is one read and there
    is no second traversal to disagree with.

    Returns a NEW corpus dict with a new section list; ``corpus`` is never
    mutated, because the base is the import-time ``HELP_CORPUS`` reused by every
    swap. When nothing is merged the base is returned unchanged.

    NEVER RAISES — which is why this exists instead of a call into
    ``build_mod_sections`` (that one raises BuildError on a duplicate slug, and
    a careless installed mod must not be able to blank the Help window). A
    record whose ``help_md`` is absent, not a str, blank, oversized or
    unparseable is skipped; anything unforeseen degrades to the unmerged corpus.
    """
    global _installed_cards
    try:
        base = list(corpus.get("sections") or [])
        seen = {sec.get("slug") for sec in base if isinstance(sec, dict)}
        records = (index or {}).get("mods") or {}
        cards_out: dict = {}
        added: list = []
        for offset, mod_id in enumerate(sorted(records)):
            try:
                section = _installed_section(mod_id, records[mod_id], offset,
                                             cards_out)
            except Exception:  # noqa: BLE001 - one bad mod, not a blank Help
                LOGGER.warning("installed mod %s: help.md not merged",
                               mod_id, exc_info=True)
                continue
            if section is None:
                continue
            # Unreachable while ids are "x-"-prefixed and slugs are forced to
            # them (see _installed_section); kept because "never raises" must
            # also mean "never silently emits two sections under one slug".
            if section["slug"] in seen:
                LOGGER.warning("installed mod %s: help slug already taken",
                               mod_id)
                continue
            seen.add(section["slug"])
            added.append(section)
        _installed_cards = cards_out
        if not added:
            return corpus
        return {"sections": sorted(base + added, key=_section_sort_key)}
    except Exception:  # noqa: BLE001 - Help degrades, never blanks
        LOGGER.warning("installed mod help could not be merged", exc_info=True)
        return corpus


def serialize_corpus(corpus: dict) -> bytes:
    """Canonical bytes for a corpus — one form for generation AND the drift test.

    Deterministic across platforms: sorted keys, no ASCII escaping, compact
    separators, trailing newline. List order (sections/cards/blocks/spans) is
    preserved by json; only object KEY order is normalized.
    """
    text = json.dumps(corpus, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return (text + "\n").encode("utf-8")


def load_corpus() -> dict:
    """Build the corpus for serving — protective, never raises.

    Live-parse the wiki + mod help.md files when the wiki is present; else the
    packaged JSON (which bakes in the mod sections too — see __main__); else
    empty. A broken mod help.md degrades to the packaged fallback rather than
    blanking Help.
    """
    try:
        if WIKI_DIR.is_dir():
            return build_full_corpus()
    except Exception:  # noqa: BLE001 - Help must degrade, not break startup
        pass
    try:
        if PACKAGED_JSON.is_file():
            return json.loads(PACKAGED_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {"sections": []}


# Built once at import (like INDEX_HTML); edits to wiki/ need a broker restart.
HELP_CORPUS = load_corpus()


if __name__ == "__main__":  # regenerate the packaged fallback from wiki/ + mods
    data = serialize_corpus(build_full_corpus())
    PACKAGED_JSON.write_bytes(data)
    print("wrote %s (%d bytes)" % (PACKAGED_JSON, len(data)))
