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
                "tier": "dev",          # OPTIONAL: developer/operator page.
                                        # ABSENT means the end-user guide, so a
                                        # user-tier section is byte-unchanged.
                "cards": [ card, ... ] }
    card    = { "title": str,
                "body": [ block, ... ],
                "search": str }         # precomputed lowercased plain text
    block   = { "t": "p"|"bullet"|"sub"|"pre", "spans": [ span, ... ] }
    span    = { "t": "text"|"strong"|"code"|"kbd", "v": str }

The frontend renders only this fixed, known set of types; anything the parser
cannot classify degrades to a plain ``text`` span — NEVER to raw HTML. This
keeps the long-standing XSS-safety invariant of the Help renderer intact even
though the content now comes from files.

``build_corpus`` is strict: it RAISES on structural problems (unbalanced
``help:ignore`` markers, an unclosed code fence, an ignore region that crosses
a fence boundary, a malformed ``help:tier`` front-matter marker, duplicate
slugs) so tests / the regeneration step catch them. ``load_corpus`` (used at import) is protective: it parses the live
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

    Emphasis is recognised BEFORE the code split, so a ``**bold `code` span**``
    keeps its emphasis instead of rendering its ``**`` markers as literal
    asterisks. Splitting on code first put the opener and the closer into two
    different non-code parts, where neither could ever pair — visible on
    essentially every developer page.

    The span model is FLAT (no nesting), so such a region is emitted as the code
    span plus ``strong`` spans for the text AROUND it; the code span keeps its
    own visual treatment, which it already had.
    """
    spans: list[dict] = []
    # ONE left-to-right pass for the emphasis regions, up front: re-scanning
    # from each new position instead would be quadratic on a paragraph that
    # holds many code spans ahead of one such region, and this parses at most
    # 256 KiB of installed-mod help.md on the event loop (see below).
    bolds = _bold_with_code(text)
    i = 0
    pos = 0
    n = len(text)
    while pos < n and i < len(bolds):
        bold = bolds[i]
        if bold.start() < pos:
            i += 1              # already consumed (it sat inside a code span)
            continue
        code = _CODE.search(text, pos)
        if code is not None and code.start() < bold.start():
            # A code span OPENS before the emphasis does, so it wins — exactly
            # as it did when the whole line was split on code up front. Emit
            # through its end and look again from there; that keeps a `**`
            # sitting inside a code span literal, and keeps a straddling
            # backtick from stealing text out of a code span.
            spans.extend(_split_code(text[pos:code.end()], search_extra))
            pos = code.end()
            continue
        spans.extend(_split_code(text[pos:bold.start()], search_extra))
        spans.extend(_emphasized(bold.group(1), search_extra))
        pos = bold.end()
        i += 1
    spans.extend(_split_code(text[pos:], search_extra))
    return _coalesce(spans)


def _bold_with_code(text: str) -> list:
    """Every ``**...**`` in ``text`` whose content holds a code span.

    ``_BOLD``'s own non-overlapping left-to-right scan decides the pairing, so
    this sees the same emphasis regions ``_parse_non_code`` always has. Emphasis
    WITHOUT a code span is left to it (via the ordinary code split) so its
    output is byte-unchanged: this is a fix for one broken construct, not a new
    emphasis parser. ``_BOLD``'s ``[^*]+`` also means an unpaired ``**`` — and
    one wrapped around a nested ``*italic*`` — never matches here and stays
    exactly as literal as it is today.
    """
    return [m for m in _BOLD.finditer(text) if _CODE.search(m.group(1))]


def _split_code(text: str, search_extra: list[str] | None) -> list[dict]:
    """The historical parse: split on code spans (their content is literal,
    never nested-parsed) and run everything else through ``_parse_non_code``."""
    spans: list[dict] = []
    for i, part in enumerate(_CODE.split(text)):
        if i % 2 == 1:
            spans.append(_code_span(part))
        else:
            spans.extend(_parse_non_code(part, search_extra))
    return spans


def _emphasized(content: str, search_extra: list[str] | None) -> list[dict]:
    """One ``**...**`` region that contains code -> flat spans.

    Code parts stay code (or kbd); the text around them becomes ``strong``.
    Those text parts still go through ``_parse_non_code``, so entities are
    decoded and a wiki/md link inside bold renders its label and feeds
    ``search_extra`` rather than being silently dropped. Nested bold is
    impossible (``_BOLD``'s content excludes ``*``), so nothing here can come
    back as anything but text.
    """
    spans: list[dict] = []
    for i, part in enumerate(_CODE.split(content)):
        if i % 2 == 1:
            spans.append(_code_span(part))
            continue
        # One run of emphasised text per part: _coalesce only merges `text`
        # spans, so a part that came back as several (a wiki link plus its
        # surrounding prose) would otherwise emit adjacent identical `strong`
        # spans.
        run: list[dict] = []
        for span in _parse_non_code(part, search_extra):
            if span["t"] != "text":       # defensive: nothing emits one today
                run.append(span)
            elif run and run[-1]["t"] == "strong":
                run[-1]["v"] += span["v"]
            else:
                run.append({"t": "strong", "v": span["v"]})
        spans.extend(run)
    return spans


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
# Fenced code: ONE recogniser, shared by every consumer.
#
# Three things need to know where a fence starts and stops: the page splitter
# (a ``##`` inside a fence is code, not a card boundary), the block parser (a
# fence becomes one verbatim ``pre``), and the help:ignore stripper (a region
# may not rewrite a page's fence topology). Two hand-rolled copies of "does this
# line open a fence?" WILL drift on indentation / fence length / closing syntax,
# and the drift shows up as a page silently cut in half. So there is exactly one
# rule, stated once, here.
# --------------------------------------------------------------------------- #

_FENCE_MARK = "```"


def _is_fence(line: str) -> bool:
    """True if ``line`` is a code-fence delimiter (opening OR closing).

    THE fence rule for this module. Deliberately the same permissive test the
    block parser has always used — a stripped line starting with three
    backticks — so making it shared changes no existing page. Anything stricter
    (CommonMark's "the closing fence must be at least as long as the opener",
    ``~~~`` fences) belongs here and nowhere else.
    """
    return line.strip().startswith(_FENCE_MARK)


def _fence_ids(lines: list[str]) -> tuple[list[int], int | None]:
    """Map each line to its fence block: 0 = outside, N > 0 = inside the Nth
    fence (delimiter lines included).

    Returns ``(ids, unclosed)`` where ``unclosed`` is the 0-based index of an
    opening fence that never closed, else ``None``. The caller decides whether
    that is fatal, because ``parse_blocks`` has always tolerated it and the page
    splitter must not.
    """
    ids = [0] * len(lines)
    block = 0
    opened_at: int | None = None
    for i, line in enumerate(lines):
        if _is_fence(line):
            if opened_at is None:
                block += 1
                opened_at = i
            else:
                opened_at = None
            ids[i] = block
        elif opened_at is not None:
            ids[i] = block
    return ids, opened_at


def _source_prefix(source: str | None) -> str:
    """``"Getting-Started.md: "`` when we know the file, else ``""``."""
    return "%s: " % source if source else ""


# --------------------------------------------------------------------------- #
# Block parsing: the raw lines of one card -> a list of typed blocks.
# --------------------------------------------------------------------------- #

def _ignore_pairs(lines: list[str], ids: list[int],
                  source: str | None = None) -> list[tuple[int, int]]:
    """Pair every help:ignore marker and classify it against the fence map.

    Returns the ``(start, end)`` line-index pairs that are REAL directives —
    both markers outside any fence. Two other outcomes:

    Only a marker OUTSIDE every fence is a directive. A marker inside a fenced
    block is documentation ABOUT the marker — which is exactly how the
    mod-authoring page documents this system, one marker per code sample — so it
    is literal text and is never paired, never stripped, and never unbalanced.

    That rule also makes a fence-crossing region unrepresentable rather than
    merely rejected: both markers of a real region are outside fences, so every
    fence between them is necessarily complete and stripping can never rewrite
    the page's fence topology. A start whose only candidate end is buried in a
    fence is reported as the missing end that it is, with the buried line named.
    """
    pfx = _source_prefix(source)
    stack: list[int] = []
    real: list[tuple[int, int]] = []

    def _buried(marker: str) -> str:
        at = [j + 1 for j, l in enumerate(lines)
              if ids[j] != 0 and l.strip() == marker]
        if not at:
            return ""
        return ("; a %s at line %s is inside a code fence, where it is literal "
                "text rather than a directive"
                % (marker, ", ".join(str(a) for a in at)))

    for i, line in enumerate(lines):
        if ids[i] != 0:
            continue                      # inside a fence: literal code
        stripped = line.strip()
        if stripped == _IGNORE_START:
            stack.append(i)
        elif stripped == _IGNORE_END:
            if not stack:
                raise BuildError("%sunbalanced help:ignore (end before start) "
                                 "at line %d%s"
                                 % (pfx, i + 1, _buried(_IGNORE_START)))
            real.append((stack.pop(), i))
    if stack:
        raise BuildError("%sunbalanced help:ignore (missing %d end marker(s); "
                         "last start at line %d)%s"
                         % (pfx, len(stack), stack[-1] + 1,
                            _buried(_IGNORE_END)))
    return real


def _strip_ignored(text: str, source: str | None = None) -> str:
    """Remove <!-- help:ignore-start --> .. <!-- help:ignore-end --> regions.

    Raises BuildError on unbalanced markers so a typo can't silently erase the
    rest of a page, on an unclosed code fence, and on a region that crosses a
    fence boundary (see ``_ignore_pairs``). A fence wholly inside a region is
    fine — the whole region disappears, fence and all.
    """
    lines = text.splitlines()
    ids, unclosed = _fence_ids(lines)
    if unclosed is not None:
        raise BuildError("%sunclosed code fence opened at line %d"
                         % (_source_prefix(source), unclosed + 1))
    drop = [False] * len(lines)
    for start, end in _ignore_pairs(lines, ids, source):
        for k in range(start, end + 1):
            drop[k] = True
    return "\n".join(line for k, line in enumerate(lines) if not drop[k])


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


def _dedent_fence_line(line: str, indent: int) -> str:
    """Drop a CRLF artifact and up to ``indent`` leading spaces from one line.

    ``indent`` is the opening fence delimiter's own indentation. Removing it is
    what CommonMark specifies for a fence nested inside a list item; removing
    only up to it is what keeps relative indentation inside the block intact.
    Tabs are never consumed — a tab's width is not knowable here, and guessing
    would corrupt exactly the Makefile / YAML content this must preserve.
    """
    if line.endswith("\r"):
        line = line[:-1]
    cut = 0
    while cut < indent and cut < len(line) and line[cut] == " ":
        cut += 1
    return line[cut:]


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
        # Fenced code block. Open AND close go through _is_fence — the same
        # predicate the page splitter and the ignore stripper use — so the three
        # can never disagree about where a fence begins or ends.
        if _is_fence(line):
            flush_para()
            code_lines = []
            # CommonMark: the opening fence's own indentation is layout (the
            # fence sits inside a list item), not content. Strip up to that many
            # leading spaces from each line so a fence nested under a bullet is
            # not rendered shifted right — while a column-0 fence holding an
            # indented ASCII diagram keeps every space it had.
            indent = len(line) - len(line.lstrip())
            i += 1
            while i < n and not _is_fence(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            # A `pre` block is VERBATIM source: join the raw lines with "\n" and
            # never strip them individually. Leading whitespace is content for
            # everything that lands in a fence (systemd units, YAML, JSON, shell
            # continuations), so `" ".join(l.strip() ...)` — and equally
            # `"\n".join(l.strip() ...)` — would destroy the block. The only
            # normalizations are the newline convention (a trailing "\r" left by
            # CRLF input is line-ending machinery) and the opening fence's own
            # indentation, removed above.
            code = "\n".join(_dedent_fence_line(l, indent) for l in code_lines)
            # Emptiness is decided from the STRIPPED text (a fence holding only
            # blank lines is still not a block) while the UNSTRIPPED text is what
            # gets stored.
            if code.strip():
                blocks.append({"t": "pre", "spans": [{"t": "code", "v": code}]})
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


_H2 = re.compile(r"##\s+(.*)")


def _page_chunks(text: str, source: str | None = None) -> list[str]:
    """Strip ignore regions, then split the page on level-2 headings.

    Returns the same shape ``re.split(r"(?m)^##\\s+(.*)$", text)`` returns —
    ``[intro, title, body, title, body, ...]`` — but from a LINE-STATE SCANNER
    that knows about fences, so a ``## Not A Heading`` inside a fenced block
    stays code instead of silently cutting the page in two. That regex ran over
    the whole page before ``parse_blocks`` ever saw a fence; no shipped page
    tripped it, but a page of curl/systemd/JSON examples is exactly where the
    first one appears.

    Raises BuildError on an unclosed fence (turning the rest of a developer page
    into code is worse than failing the build).
    """
    text = _strip_ignored(text, source)
    lines = text.split("\n")
    ids, unclosed = _fence_ids(lines)
    if unclosed is not None:
        # Unreachable via _strip_ignored (it raises first, on the ORIGINAL line
        # numbers); kept so a direct caller can't get a half-parsed page.
        raise BuildError("%sunclosed code fence opened at line %d"
                         % (_source_prefix(source), unclosed + 1))
    chunks: list[str] = []
    cut = 0    # start offset of the chunk being accumulated
    off = 0    # start offset of the current line
    for i, line in enumerate(lines):
        m = _H2.fullmatch(line) if ids[i] == 0 else None
        if m:
            chunks.append(text[cut:off])
            chunks.append(m.group(1))
            cut = off + len(line)
        off += len(line) + 1   # +1 for the "\n" that split() consumed
    chunks.append(text[cut:])
    return chunks


# --------------------------------------------------------------------------- #
# Front matter: <!-- help:tier dev -->
#
# A page declares WHO IT IS FOR, so the Help window can show the end-user guide
# by default and reveal developer/operator pages only behind a checkbox. The
# default is ``user`` and emits no key at all.
#
# A magic HTML comment is only safe if it is parsed as STRICT FRONT MATTER. The
# failure this must not have is a mistyped or misplaced marker being shrugged
# off, because that silently publishes internals into the end-user guide. So the
# marker must stand ALONE on the page's first meaningful line (leading blank
# lines are fine), appear EXACTLY ONCE, and carry a RECOGNISED value; every
# other shape raises BuildError naming the file, the line and the value. There
# is deliberately no "last wins" and no silent ignore.
#
# It is consumed BEFORE the page splitter and the block parser see the page: a
# surviving marker renders as visible text, and on a page that would otherwise
# have no intro it conjures an Overview card out of nothing.
# --------------------------------------------------------------------------- #

_TIER_DEFAULT = "user"
_TIERS = ("user", "dev")

# ONE pattern, used two ways: ``search`` finds an OCCURRENCE anywhere on a line
# (what makes a duplicate or a misplaced marker loud instead of silent), and
# ``fullmatch`` against the stripped line is the STANDALONE test. A second
# pattern for the second job is how the two would come to disagree about what
# even counts as a marker.
#
# It matches the COMMENT form only, so a page may still discuss "help:tier" in
# prose, and (exactly like help:ignore) may show the literal marker by putting
# it in a fenced block. group(1) is the raw value — deliberately anything, so an
# empty or unknown one gets the same "unrecognised value" error rather than
# being mistaken for "not a marker at all".
_TIER_MARKER = re.compile(r"<!--\s*help:tier\b(.*?)-->")


def _take_tier(text: str, source: str | None = None) -> tuple[str, str]:
    """Split a page into ``(tier, body)``, consuming the front-matter marker.

    ``text`` must already have had its help:ignore regions stripped — a marker
    inside an ignored region has no effect, which falls out of it never reaching
    here. A marker inside a code fence is literal code and neither sets the tier
    nor is stripped: the fence question is answered by ``_fence_ids``, the same
    one recogniser the splitter, the block parser and the ignore stripper use.
    """
    pfx = _source_prefix(source)
    lines = text.split("\n")
    # The unclosed flag is dropped on purpose: _strip_ignored already raised on
    # it, and it runs ahead of this on every path in.
    ids, _unclosed = _fence_ids(lines)
    hits = [i for i, line in enumerate(lines)
            if ids[i] == 0 and _TIER_MARKER.search(line)]
    if not hits:
        return _TIER_DEFAULT, text
    if len(hits) > 1:
        raise BuildError("%shelp:tier declared %d times (lines %s); it is front "
                         "matter — exactly one, on the first line"
                         % (pfx, len(hits),
                            ", ".join(str(i + 1) for i in hits)))
    at = hits[0]
    first = next((i for i, line in enumerate(lines) if line.strip()), at)
    if at != first:
        raise BuildError("%shelp:tier at line %d is below the first content "
                         "line (line %d); it is front matter and must come "
                         "before any body content" % (pfx, at + 1, first + 1))
    m = _TIER_MARKER.fullmatch(lines[at].strip())
    if m is None:
        raise BuildError("%shelp:tier at line %d must stand alone on its line "
                         "(it is a directive, not prose), got %r"
                         % (pfx, at + 1, lines[at].strip()))
    value = m.group(1).strip()
    if value not in _TIERS:
        raise BuildError("%sunrecognised help:tier %r at line %d (expected one "
                         "of: %s)" % (pfx, value, at + 1, ", ".join(_TIERS)))
    return value, "\n".join(lines[:at] + lines[at + 1:])


def parse_page(text: str, source: str | None = None) -> list[dict]:
    """Parse one wiki page's markdown into a list of cards.

    The intro (text before the first ## heading) becomes an "Overview" card;
    each ## heading becomes a card; a trailing cross-nav card is dropped.
    ``source`` is the page's name, used only to name the file in BuildError.

    Any ``help:tier`` front matter is consumed (so it never renders) and its
    value discarded; callers that need it use ``parse_page_with_tier``.
    """
    return parse_page_with_tier(text, source)[1]


def parse_page_with_tier(text: str, source: str | None = None) \
        -> tuple[str, list[dict]]:
    """``(tier, cards)`` for one page — the full ``parse_page`` result.

    Split from ``parse_page`` rather than changing its return type because the
    tier belongs to the SECTION and most callers only want cards.
    """
    page = _strip_ignored(text, source)
    tier, page = _take_tier(page, source)
    # _page_chunks strips ignore regions itself; doing it again on already
    # stripped text is a no-op (whole marker lines are gone, and the only ones
    # that can remain are fenced, which it leaves alone), and running the
    # stripper FIRST here is what lets the tier scan see the page the splitter
    # will actually see.
    chunks = _page_chunks(page, source)
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
    return tier, out


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
        tier, cards = parse_page_with_tier(path.read_text(encoding="utf-8"),
                                           path.name)
        if not cards:
            continue
        section = {"slug": slug, "label": label, "order": order,
                   "cards": cards}
        # ONLY a dev page carries the key. A user-tier section keeps the exact
        # bytes it has today, so the default view — and the unauthenticated
        # /help-corpus.json payload behind it — is unchanged by this feature.
        if tier != _TIER_DEFAULT:
            section["tier"] = tier
        sections.append(section)

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
    that has BOTH ``mod.json`` and ``help.md``: parse ``help.md`` through the
    same path the wiki uses — including ``help:tier`` front matter, which a mod
    declares exactly the way a wiki page does — and skip it if that yields no
    cards. The optional
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
        tier, cards = parse_page_with_tier(
            (mod_dir / "help.md").read_text(encoding="utf-8"),
            "mods/%s/help.md" % mod_dir.name)
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
        # Same rule as the wiki (a mod's help.md IS a wiki page): dev-only key.
        if tier != _TIER_DEFAULT:
            section["tier"] = tier
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
#
# What comes out of here MUST NOT BE SERVED UNAUTHENTICATED (#173).
# ``GET /help-corpus.json`` stays publicly REACHABLE, but it hands a caller with
# no token the unmerged base and only a caller holding one the merged corpus —
# otherwise the ids of installed mods that ship help, their help text and their
# manifest's label/icon are enumerable by anyone who can reach the port, and
# this is the only surface that exposes installed help at all (the asset route
# cannot serve help.md). Keeping the merge OFF the base object is what makes
# that possible: the caller (app._swap_mods_index) keeps both, so never mutate
# ``corpus`` in place here.
# --------------------------------------------------------------------------- #

# Installed sections sort after the wiki (orders are small) AND after the
# shipped mods (_MOD_ORDER_BASE + n). A mod may still override with help.order.
_INSTALLED_ORDER_BASE = 3000
# modinstall caps every file at 256 KiB and the store at 32 mods, on the install
# path AND the scan path, and a str is never longer than its UTF-8 encoding — so
# both ceilings are already enforced upstream. They are restated locally, looser
# rather than tighter, only so that a HAND-BUILT index cannot hand the parser an
# unbounded string or an unbounded number of them. They are not a second policy,
# and they are what bounds the worst-case parse this does on the event loop
# (32 x 256 KiB, and only for mods whose help text is not already cached).
_MAX_INSTALLED_HELP_CHARS = 256 * 1024
_MAX_INSTALLED_SECTIONS = 32

# help.md text -> parsed cards, rebuilt to EXACTLY the currently-merged set on
# every call. Parsing is a pure function of the text, so a content key can never
# go stale; rebuilding rather than accumulating means the cache holds the same
# card objects the live corpus already holds, so it costs no steady-state
# memory and cannot outgrow the index. It earns its keep because a swap runs ON
# THE EVENT LOOP under mods_install_lock: without it, one install would re-parse
# every other installed mod's help before the loop turns. Measured at the
# ceiling (32 mods x 251 KiB of DISTINCT help): 1650 ms cold, 1 ms to add one
# more mod. The cold parse itself is paid at create_app, before the loop exists;
# what is left on the loop is a rescan that finds every help.md changed, which
# is an operator editing 8 MiB of their own markdown and is priced accordingly.
#
# Two mods with byte-identical help.md therefore SHARE one cards list, as do a
# section and its predecessor across a swap. That is safe because cards are
# write-once plain data — the corpus is only ever serialized, never annotated.
# If that ever stops being true, key the cache by (id, text) and copy.
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
        # parse_page, not parse_page_with_tier: any help:tier front matter is
        # still CONSUMED (so it never renders), but an installed mod does not
        # get to tag its section yet — that is a separate decision from the
        # shipped wiki/mod tiers, and a malformed marker here is a per-mod skip
        # by way of the caller's guard, never a raise.
        cards = parse_page(help_md, "%s/help.md" % mod_id)
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
    swap AND served verbatim to every unauthenticated caller (#173) — mutating
    it would leak the installed sections into the public response. When nothing
    is merged the base is returned unchanged.

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
        # Filter to str keys BEFORE sorting: a hand-built index carrying one
        # non-str key would otherwise raise inside sorted() and discard every
        # OTHER mod's help along with it.
        ids = sorted(mid for mid in records if isinstance(mid, str))
        cards_out: dict = {}
        added: list = []
        for offset, mod_id in enumerate(ids[:_MAX_INSTALLED_SECTIONS]):
            try:
                section = _installed_section(mod_id, records[mod_id], offset,
                                             cards_out)
            except Exception:  # noqa: BLE001 - one bad mod, not a blank Help
                LOGGER.warning("installed mod %s: help.md not merged",
                               mod_id, exc_info=True)
                continue
            if section is None:
                continue
            # Unreachable, and guarded on the OTHER side too: an installed id
            # must start with "x-", and test_no_shipped_help_slug_is_in_the_
            # installed_namespace pins that no wiki page stem and no shipped
            # mod's help.slug ever does. Kept anyway because "never raises" has
            # to also mean "never silently emits two sections under one slug" —
            # and note the base wins, so a collision loses the INSTALLED help.
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
