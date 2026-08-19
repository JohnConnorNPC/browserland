"""Static inventory of the mod-facing ``ctx`` surface (#203 §4).

WHAT THIS IS. A line-scanner over the mod-loader fragment FAMILY -- every
``ui._ORDERED`` entry matching ``86*_js_mod_*.js`` -- that lists the member
paths a mod's ``init(ctx)`` can reach: ``id``, ``settings.text``,
``serverStore.getRevision``, ``dialog.open``, plus the ``info.*`` members the
terminal bag carries. The result is written to the checked-in
``ctx_inventory.json``; ``python -m webterm.broker.ctx_inventory``
regenerates it, and ``tests/test_ctx_inventory.py`` regenerates-and-diffs it
byte-exact, exactly the way ``test_help_corpus.py`` pins the help corpus.

WHY IT IS STATIC. CI never executes UI JavaScript, so nothing here runs the
loader; it reads text. Two shapes are scanned:

1. ``makeCtx``'s own ctx object literal in ``86_js_mod_loader.js`` -- ctx v1.
2. The ctx-EXTENDER functions (#194). ``_registerCtxExtender(_ctxFoo)`` names a
   plain function whose body assigns onto ``ctx`` -- or onto a local bound to a
   ctx family -- which is precisely what makes an extender-added member
   statically findable. Every fragment after 86c adds its surface this way.

The fragment list is DISCOVERED from ``ui._ORDERED`` at scan time, never
hardcoded: the family has grown by ten fragments in six checkpoints, so a
hardcoded list is wrong by construction.

IT FAILS LOUD. An unparseable ctx literal, an extender whose function body
cannot be found or brace-matched, or an extender that yields no members at all
raises ``CtxScanError``. A scanner that silently returns FEWER members is worse
than no scanner -- it turns a missed member into a green build.

HONEST LIMITS. This enforces member-NAME coverage over ``ctx`` and ``info.*``
only -- not prose accuracy, not signatures, not semantics. Nesting is one level
deep (``settings.text``, not ``settings.text().set``). ``registerMod`` fields
are explicitly OUT OF SCOPE. A member installed by machinery other than the two
shapes above (a computed key, or a member added by a helper an extender calls)
is not seen; the wiki cross-check in the test is the backstop that keeps the
listing honest for the shapes it does cover.

PLATFORM. Fragments are read as UTF-8 with newlines normalized to ``\\n``
before scanning, so a CRLF working copy (this repo ships ``.gitattributes`` /
autocrlf) produces byte-identical output on Windows and Linux.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path

_DIR = Path(__file__).resolve().parent

#: The checked-in listing this module regenerates.
INVENTORY_JSON = _DIR / "ctx_inventory.json"

#: Which ``_ORDERED`` entries make up the loader family (a glob, not a list).
FAMILY_GLOB = "86*_js_mod_*.js"


class CtxScanError(RuntimeError):
    """Raised when a ctx literal or an extender body cannot be parsed.

    Loud on purpose: a silent partial scan converts a missed ctx member into a
    passing drift test.
    """


# --------------------------------------------------------------------------- #
# reading + masking
# --------------------------------------------------------------------------- #

def read_fragment(path: Path) -> str:
    """Read a fragment as text with newlines normalized to ``\\n``.

    Explicit encoding and explicit normalization: the same checkout is CRLF on
    Windows and LF on Linux, and the inventory bytes must not depend on that.
    """
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace(
        "\r", "\n")


_REGEX_PREV = set("(,=:[!&|?{};+-*%~^<>")
_REGEX_PREV_WORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "do", "else", "case", "yield", "await",
}


def mask_source(src: str) -> str:
    """Blank out comment and string INTERIORS, keeping length and newlines.

    The result lines up index-for-index with ``src``, so a brace found in the
    mask is real code (never one inside a comment or a string) while the text
    itself can still be sliced out of ``src``. Delimiters are kept; only the
    contents become spaces. Template literals are masked whole, ``${...}``
    included -- nothing this scanner looks for is ever written inside one.
    """
    out = list(src)
    i, n = 0, len(src)
    prev = ""
    prev_word = ""

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c == "/" and src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise CtxScanError("unterminated block comment")
            blank(i, j + 2)
            i = j + 2
            continue
        if c in "'\"`":
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == c:
                    break
                if src[j] == "\n" and c != "`":
                    raise CtxScanError("unterminated string literal")
                j += 1
            if j >= n:
                raise CtxScanError("unterminated string/template literal")
            blank(i + 1, j)
            i = j + 1
            prev, prev_word = c, ""
            continue
        if c == "/" and (prev == "" or prev in _REGEX_PREV
                         or prev_word in _REGEX_PREV_WORDS):
            j, cls, ok = i + 1, False, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    break
                if ch == "[":
                    cls = True
                elif ch == "]":
                    cls = False
                elif ch == "/" and not cls:
                    ok = True
                    break
                j += 1
            if ok:
                blank(i + 1, j)
                i = j + 1
                prev, prev_word = "/", ""
                continue
            # not a regex after all -- fall through, it is a division operator
        if not c.isspace():
            prev = c
            prev_word = prev_word + c if (c.isalnum() or c in "_$") else ""
        i += 1
    return "".join(out)


def match_brace(mask: str, open_idx: int) -> int:
    """Index of the ``}`` closing the ``{`` at ``open_idx`` in a masked source."""
    if open_idx < 0 or open_idx >= len(mask) or mask[open_idx] != "{":
        raise CtxScanError("expected '{' at offset %d" % open_idx)
    depth = 0
    for i in range(open_idx, len(mask)):
        if mask[i] == "{":
            depth += 1
        elif mask[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise CtxScanError("unbalanced braces from offset %d" % open_idx)


def _indent_of(mask: str, idx: int) -> int:
    """Indent (in spaces) of the line containing ``idx``."""
    start = mask.rfind("\n", 0, idx) + 1
    line = mask[start:]
    return len(line) - len(line.lstrip(" "))


# --------------------------------------------------------------------------- #
# object-literal member extraction (indentation-based, one level deep)
# --------------------------------------------------------------------------- #

_KEY_RE = re.compile(r"^(?P<ind>[ ]*)(?P<key>[A-Za-z_$][A-Za-z0-9_$]*)\s*:")


def literal_members(mask: str, open_idx: int, prefix: str = "",
                    depth: int = 1) -> list[str]:
    """Member paths of the object literal whose ``{`` sits at ``open_idx``.

    Indentation-based, because every fragment in this family is uniformly
    4-space indented and one key per line: a key of the literal is the first
    token on a line indented exactly one step past the literal's opening line.
    A key whose value opens its own literal at the end of that line recurses
    once -- ``storage.get`` -- which is this listing's documented depth.
    """
    close = match_brace(mask, open_idx)
    want = _indent_of(mask, open_idx) + 4
    found: list[str] = []
    nl = mask.find("\n", open_idx)
    if nl < 0 or nl > close:
        return found  # single-line literal: nothing to walk line-wise
    pos = nl + 1
    while pos < close:
        eol = mask.find("\n", pos)
        if eol < 0 or eol > close:
            eol = close
        line = mask[pos:eol]
        m = _KEY_RE.match(line)
        if m and len(m.group("ind")) == want:
            key = m.group("key")
            path = "%s.%s" % (prefix, key) if prefix else key
            found.append(path)
            stripped = line.rstrip()
            if depth > 0 and stripped.endswith("{"):
                found.extend(literal_members(
                    mask, pos + len(stripped) - 1, path, depth - 1))
        pos = eol + 1
    return found


# --------------------------------------------------------------------------- #
# shape 1 -- makeCtx's own ctx literal
# --------------------------------------------------------------------------- #

_CTX_LITERAL_RE = re.compile(r"\bconst\s+ctx\s*=\s*\{")


def scan_ctx_literal(src: str, mask: str, where: str) -> list[str]:
    """Members of ``makeCtx``'s ``const ctx = {...}`` literal."""
    if "function makeCtx(" not in mask:
        raise CtxScanError("%s: makeCtx not found" % where)
    m = _CTX_LITERAL_RE.search(mask)
    if not m:
        raise CtxScanError(
            "%s: could not locate makeCtx's `const ctx = {` literal" % where)
    members = literal_members(mask, m.end() - 1)
    if not members:
        raise CtxScanError(
            "%s: the ctx literal parsed to ZERO members -- refusing to emit an "
            "empty inventory" % where)
    return members


# --------------------------------------------------------------------------- #
# shape 2 -- the ctx extenders (#194)
# --------------------------------------------------------------------------- #

_REGISTER_RE = re.compile(
    r"(?<!function )"
    r"_registerCtxExtender\s*\(\s*"
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\)")

#: ``const fam = ... ctx.<family> ...`` -- an extender's alias for a family it
#: decorates in place (the "never replace a family" rule at 86c).
_ALIAS_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*([^;\n]*(?:\n[^;]*)?);")

_CTX_MEMBER_RE = re.compile(
    r"\bctx\.([A-Za-z_$][A-Za-z0-9_$]*)"
    r"(?:\.([A-Za-z_$][A-Za-z0-9_$]*))?\s*=(?!=)")

#: Matched against the MASK, so the key itself is read out of the source at the
#: same offset -- masking blanks string interiors.
_DEFINE_PROP_RE = re.compile(
    r"Object\.defineProperty\s*\(\s*ctx\s*,\s*(?P<q>['\"])")

_INFO_MEMBER_RE = re.compile(
    r"\binfo\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=(?!=)")


def _find_function_body(mask: str, name: str) -> tuple[int, int]:
    """``(open_brace, close_brace)`` of ``function <name>(...) {`` in ``mask``."""
    m = re.search(r"\bfunction\s+%s\s*\(" % re.escape(name), mask)
    if not m:
        raise CtxScanError("extender function %r has no declaration" % name)
    brace = mask.find("{", m.end())
    if brace < 0:
        raise CtxScanError("extender function %r has no body" % name)
    return brace, match_brace(mask, brace)


def _alias_families(body: str) -> dict[str, str]:
    """Locals bound to a ctx family: ``{localName: familyName}``.

    Only an unambiguous binding counts -- every ``ctx.<x>`` in the initializer
    must name the SAME family -- so ``const fam = ctx.hosts || {}`` and
    ``const store = ctx && ctx.serverStore;`` both resolve, and an initializer
    mentioning two families resolves to nothing rather than to a guess.
    """
    out: dict[str, str] = {}
    for m in _ALIAS_RE.finditer(body):
        local, init = m.group(1), m.group(2)
        fams = set(re.findall(r"\bctx\.([A-Za-z_$][A-Za-z0-9_$]*)", init))
        if len(fams) == 1:
            out[local] = fams.pop()
    return out


def scan_extender(src: str, mask: str, name: str, where: str) -> list[str]:
    """Member paths an extender function installs on the ctx it is handed."""
    open_b, close_b = _find_function_body(mask, name)
    body = mask[open_b:close_b + 1]
    body_src = src[open_b:close_b + 1]
    found: list[str] = []

    for m in _CTX_MEMBER_RE.finditer(body):
        fam, sub = m.group(1), m.group(2)
        path = "%s.%s" % (fam, sub) if sub else fam
        found.append(path)
        if sub is None:
            # `ctx.prefs = {` / `ctx.assets = Object.freeze({` -- the family's
            # own members are one level down, same depth rule as the v1 literal.
            line_end = body.find("\n", m.end())
            line_end = len(body) if line_end < 0 else line_end
            head = body[m.end():line_end].rstrip()
            if head.endswith("{"):
                found.extend(literal_members(
                    mask, open_b + m.end() + len(head) - 1, fam, 0))

    for m in _DEFINE_PROP_RE.finditer(body):
        end = body_src.find(m.group("q"), m.end())
        if end < 0:
            raise CtxScanError(
                "%s: %s has an Object.defineProperty(ctx, ...) whose key is "
                "not a simple string literal" % (where, name))
        found.append(body_src[m.end():end])

    aliases = _alias_families(body)
    for local, fam in aliases.items():
        for m in re.finditer(
                r"\b%s\.([A-Za-z_$][A-Za-z0-9_$]*)\s*=(?!=)" % re.escape(local),
                body):
            found.append("%s.%s" % (fam, m.group(1)))

    if not found:
        raise CtxScanError(
            "%s: extender %s installed NO ctx member the scanner could see. "
            "Either it uses a shape this scanner does not know (extend it) or "
            "the registration is dead -- refusing to under-report." % (
                where, name))
    return found


# --------------------------------------------------------------------------- #
# the scan
# --------------------------------------------------------------------------- #

def family_fragments() -> list[Path]:
    """The loader-family fragments, in ``ui._ORDERED`` order.

    Discovered, never hardcoded: a new ``86q_js_mod_*.js`` is covered the day
    it is registered in ``_ORDERED``.
    """
    from . import ui  # local import: ui builds INDEX_HTML at import time

    names = [n for n in ui._ORDERED if fnmatch.fnmatch(n, FAMILY_GLOB)]
    if not names:
        raise CtxScanError(
            "no _ORDERED entry matches %r -- the loader family cannot be empty"
            % FAMILY_GLOB)
    paths = []
    for n in names:
        p = _DIR / n
        if not p.is_file():
            raise CtxScanError("fragment %s is in _ORDERED but not on disk" % n)
        paths.append(p)
    return paths


def build_inventory() -> dict:
    """Scan the family and return the inventory document."""
    ctx_paths: set[str] = set()
    info_paths: set[str] = set()
    extenders: list[str] = []
    saw_literal = False

    for path in family_fragments():
        src = read_fragment(path)
        mask = mask_source(src)
        where = path.name

        if _CTX_LITERAL_RE.search(mask):
            ctx_paths.update(scan_ctx_literal(src, mask, where))
            saw_literal = True

        for m in _REGISTER_RE.finditer(mask):
            name = m.group(1)
            if name in extenders:
                continue
            extenders.append(name)
            ctx_paths.update(scan_extender(src, mask, name, where))

        info_paths.update(m.group(1) for m in _INFO_MEMBER_RE.finditer(mask))

    if not saw_literal:
        raise CtxScanError(
            "no fragment in the family carried makeCtx's `const ctx = {` "
            "literal -- refusing to emit a partial inventory")
    if not extenders:
        raise CtxScanError(
            "no _registerCtxExtender call found in the family -- refusing to "
            "emit an inventory that would miss every extension fragment")

    return {
        "ctx": sorted(ctx_paths),
        "info": sorted("info.%s" % p for p in info_paths),
        "extenders": sorted(extenders),
    }


def serialize_inventory(data: dict) -> bytes:
    """Deterministic bytes for the checked-in JSON (LF, trailing newline)."""
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def load_inventory() -> dict:
    """The checked-in listing (never scanned at broker import)."""
    return json.loads(INVENTORY_JSON.read_text(encoding="utf-8"))


if __name__ == "__main__":  # regenerate the checked-in listing
    payload = serialize_inventory(build_inventory())
    tmp = INVENTORY_JSON.with_suffix(".json.tmp")
    # temp-then-rename: a plain "w" open that raises mid-write truncates the
    # checked-in file and the drift test then reports the wrong thing.
    tmp.write_bytes(payload)
    tmp.replace(INVENTORY_JSON)
    print("wrote %s (%d bytes)" % (INVENTORY_JSON, len(payload)))
