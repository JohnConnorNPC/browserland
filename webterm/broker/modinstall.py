"""Runtime-installed mods: the on-disk store, its scanner, and the catalog (#163).

A shipped mod is a path in ``ui._MODS``, spliced into ``INDEX_HTML`` at import.
An INSTALLED mod never touches that: it lives in a broker-config'd directory
beside ``webterm_state.json``, is served from same-origin ``/mods/<id>/<gen>/…``
URLs, and is loaded by a separate classic ``<script src>``. So ``mods/``,
``_MODS``, the assembled bundle, its CSP hash and every drift guard around them
stay exactly as they are -- the catalog simply gains a second, LABELLED source.

Layout::

    <mods_dir>/                    # config "mods_dir", default <state dir>/webterm_mods
      .browserland-mods            # ownership marker, written on first use
      x-notes/
        CURRENT                    # {"gen": "<sha256 hex>"} -- the atomic pointer
        <gen-a>/  mod.json  notes.js  notes.css  help.md  .gen.json
        <gen-b>/                   # the previous generation, retained
      .tmp-x-notes-<rand>/         # install staging; swept
      .old-x-notes-<rand>/         # uninstall staging; swept

CONTENT-ADDRESSED GENERATIONS. ``gen`` is a sha256 over the canonical manifest
bytes plus the sorted ``(name, sha256)`` pairs, and it is IN THE URL. Two
generations are retained, so a page that started booting against generation A
can never be handed a file from generation B mid-flight, ``Cache-Control:
immutable`` is honest, and SRI has something stable to pin.

CURRENT IS THE COMMIT. Every write publishes by replacing one small file
(``_write_state_atomic``), never by renaming a directory over another. A crash
therefore leaves CURRENT naming either the old complete generation or the new
one; anything else on disk is litter with a ``.tmp-``/``.old-`` prefix.

SERVED FROM MEMORY, NOT FROM DISK. The index carries the asset BYTES, keyed
``"<id>/<gen>/<name>"``, and the route is a single dict get -- exactly
``_vendor_asset``'s pattern. A client-supplied segment can only ever hit a known
key, so traversal is unrepresentable rather than defended against; Windows
filename tricks (alternate data streams, 8.3 short names, device names, trailing
dots, case folding) cannot reach the filesystem at all. It also keeps blocking
IO off the single event loop and removes request-time TOCTOU entirely. The
mirror obligation is on the WRITE/SCAN side -- hence the filename grammar below.

MEMORY CEILING: ``MAX_MODS`` (32) x ``RETAINED_GENERATIONS`` (2) x
``MAX_TOTAL_BYTES`` (512 KiB) = **32 MiB** of resident mod bytes, worst case.

THE SCAN IS NOT A TRUST BOUNDARY, and does not pretend to be: anyone who can
write ``mods_dir`` can already write the broker's own source tree, so a
junction/hardlink/mid-scan-mutation race there is not a privilege escalation.
What the scanner does owe is COHERENCE, so it refuses symlinks and reparse
points, refuses anything whose realpath leaves ``mods_dir``, refuses a
non-``x-`` directory (which would otherwise shadow a shipped mod -- #172), and
validates every byte it captured before that generation can be served. The
recommended way to install from an archive is the API, or unpacking with the
broker stopped -- not ``/file/unzip`` into a live ``mods_dir``.

BLOCKING: everything here does filesystem IO. Call it through ``_off_loop``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

LOGGER = logging.getLogger(__name__)

# ---- names on disk ---------------------------------------------------------

#: Ownership marker. The destructive sweeps REFUSE to run in a directory that
#: does not carry it, so a ``mods_dir`` mis-set to an existing user directory
#: cannot have unrelated trees deleted out from under it.
MARKER_NAME = ".browserland-mods"
#: The atomic pointer naming the live generation AND its retained predecessor:
#: ``{"gen": "<sha256 hex>", "previous": "<sha256 hex>"|null}``.
#:
#: ``previous`` is written, not inferred. The obvious "keep the newest OTHER
#: directory by mtime" is wrong in two ways that both lose data: an install that
#: dies between the generation rename and the commit leaves a NEWER directory
#: that was never live, and it would displace the real predecessor -- so a page
#: mid-boot against that predecessor starts 404ing and the next sweep deletes
#: it; and a predecessor whose newer neighbour fails validation is never even
#: considered. CURRENT is the ONLY thing that decides which generations exist.
CURRENT_NAME = "CURRENT"
#: Broker-WRITTEN manifest. A reserved key in an install payload, so a payload
#: can never ship a manifest that disagrees with the one that was validated.
MANIFEST_NAME = "mod.json"
#: Broker-written generation metadata: ``{"gen", "installed_at", "files"}``.
#: ``installed_at`` lives here rather than being read off a filesystem mtime,
#: which a restore, a copy or a rescan would silently rewrite.
GEN_META_NAME = ".gen.json"
#: Optional wiki-format help, captured INTO the index at scan/install time (one
#: read, so a second traversal can never disagree with what is being served).
HELP_NAME = "help.md"

TMP_PREFIX = ".tmp-"
OLD_PREFIX = ".old-"

# ---- caps ------------------------------------------------------------------

MAX_MODS = 32                       # installed mods per broker
MAX_FILES = 32                      # files per mod
MAX_FILE_BYTES = 256 * 1024         # per file
MAX_TOTAL_BYTES = 512 * 1024        # per mod, all files
MAX_BODY_BYTES = 2 * 2**20          # POST /mods/install body, checked pre-parse
#: Every OTHER mod endpoint's body: an id and two booleans need nothing more.
#: json.loads runs SYNCHRONOUSLY on the one event loop, so an uncapped body on
#: /mods/uninstall or /mods/rescan would let a padded object freeze every HTTP
#: handler, WebSocket and terminal relay for as long as the parse takes.
MAX_SMALL_BODY_BYTES = 64 * 1024
#: CURRENT / mod.json / .gen.json. Capped BEFORE json.loads for the same reason
#: -- and because a scan must never pull an arbitrarily large file into memory.
MAX_META_BYTES = 64 * 1024
MAX_REQUIRES = 32
MAX_TIERS = 8
MAX_TIER_LEN = 32
MAX_VERSION_LEN = 32
MAX_TITLE_LEN = 80
MAX_DESCRIPTION_LEN = 400
MAX_ICON_LEN = 8
#: Current + the immediately-previous generation. One retained predecessor is
#: what keeps a mid-boot page coherent across a replace; more is only litter.
RETAINED_GENERATIONS = 2


def line_cap() -> int:
    """The per-file line cap, which is LITERALLY ``ui._MAX_LINES``.

    Imported rather than retyped so the installed-file rules and the
    spliced-fragment rules cannot drift apart (``tests/test_mod_install.py``
    asserts the identity). Deferred because importing ``.ui`` ASSEMBLES the
    whole page, which a headless broker must never do (#87) -- and a headless
    broker never reaches this module at all, since every route that uses it is
    ``serve_ui``-gated."""
    from .ui import _MAX_LINES
    return _MAX_LINES


# ---- error codes -----------------------------------------------------------

#: Every distinct refusal, and the HTTP status it answers with. Distinct codes
#: on purpose: "your mod was rejected" with no reason is a support ticket.
ERROR_STATUS: Dict[str, int] = {
    "too_large": 413,               # body over MAX_BODY_BYTES (pre-parse)
    "bad_json": 400,
    "bad_mod_id": 400,
    "bad_generation": 400,          # bytes do not content-address to their dir
    "reserved_id": 400,             # not "x-"-prefixed (#172)
    "id_in_use": 409,
    "not_installed": 404,
    "bad_file_name": 400,
    "reserved_file_name": 400,
    "too_many_files": 400,
    "file_too_large": 413,
    "total_too_large": 413,
    "bad_encoding": 400,            # not str / BOM / no trailing \n / surrogate
    "bad_scripts": 400,
    "bad_styles": 400,
    "bad_requires": 400,
    "bad_manifest_field": 400,
    "unknown_manifest_key": 400,
    "css_external_reference": 400,
    "too_many_mods": 409,
    "write_failed": 500,
}


class ValidationError(Exception):
    """A refusal carrying one of ``ERROR_STATUS``'s codes.

    Raised by the validator (which runs entirely in memory, before a single
    byte is written) and by the scanner (which then SKIPS that mod rather than
    failing a request)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.code, 400)


# ---- the filename grammar (Windows-safe, not a POSIX lexical check) --------

#: The bare shape. ASCII only, must START with an alphanumeric (so no leading
#: dot, hyphen or space), and capped at 64 characters. It already excludes
#: ``/`` ``\`` ``:`` ``?`` ``#`` ``%`` ``~`` spaces and every control
#: character -- the explicit checks below exist anyway, because each one is
#: load-bearing for a reason worth stating rather than inferring from a
#: character class.
_FILE_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

#: A ``gen`` is a sha256 hex digest and nothing else, on disk and in the URL.
GEN_RE = re.compile(r"\A[0-9a-f]{64}\Z")

#: Only these are servable; ``.md`` is a broker-side input (Help), never a URL.
CONTENT_TYPES = {".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}
#: What a mod may ship at all.
ALLOWED_SUFFIXES = (".js", ".css", ".md")

#: Names the broker owns. Checked case-insensitively, and BEFORE the suffix
#: rule, so ``mod.json`` reports ``reserved_file_name`` rather than the
#: bad-suffix answer it would otherwise get.
RESERVED_FILE_NAMES = {MANIFEST_NAME.casefold(), CURRENT_NAME.casefold(),
                       GEN_META_NAME.casefold()}

#: Win32 reserves these as DEVICES in every directory, with or without an
#: extension: opening ``CON.js`` opens the console, not a file. The check is on
#: the segment before the FIRST dot, because that is what Win32 looks at.
_DEVICE_STEMS = ({"con", "prn", "aux", "nul"}
                 | {f"com{i}" for i in range(1, 10)}
                 | {f"lpt{i}" for i in range(1, 10)})

#: Rejected explicitly so a name is ALWAYS a safe URL path segment. ``%`` would
#: let a name express its own percent-decoding, ``#`` a fragment and ``?`` a
#: query; ``:`` is an NTFS alternate data stream, so ``base.css:payload.js``
#: passes a naive "it's a bare name" test and writes a hidden stream on the
#: file ``base.css``. The loader still ``encodeURIComponent``s every segment.
_URL_UNSAFE = set("?#%")


def file_name_error(name: Any) -> Optional[str]:
    """``None`` if ``name`` is a legal mod filename, else the refusal code.

    A Windows-safe GRAMMAR, not a POSIX lexical check: on this platform a name
    that merely lacks ``/`` and ``..`` is not enough. See ``_URL_UNSAFE``,
    ``_DEVICE_STEMS`` and ``RESERVED_FILE_NAMES`` for what each clause buys.
    Case-collision across a SET of names is a separate check
    (``_reject_casefold_collisions``) because it is not a property of one
    name."""
    if not isinstance(name, str) or not name:
        return "bad_file_name"
    if name.casefold() in RESERVED_FILE_NAMES:
        return "reserved_file_name"
    if ":" in name:                                 # NTFS alternate data stream
        return "bad_file_name"
    if any(ch in _URL_UNSAFE for ch in name):
        return "bad_file_name"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        return "bad_file_name"
    if not _FILE_NAME_RE.fullmatch(name):
        return "bad_file_name"
    # Suffix last-dot, case-insensitively. A trailing dot ("a.js.") has no
    # allowed suffix and dies here -- which matters, because Win32 STRIPS
    # trailing dots and would collide it with "a.js".
    lowered = name.casefold()
    if not any(lowered.endswith(sfx) for sfx in ALLOWED_SUFFIXES):
        return "bad_file_name"
    if lowered.split(".", 1)[0] in _DEVICE_STEMS:
        return "bad_file_name"
    return None


def content_type(name: str) -> Optional[str]:
    """The content type this file is SERVED as, or ``None`` if it is not
    servable at all (``help.md`` and the broker-written metadata are inputs)."""
    lowered = name.casefold()
    for suffix, ctype in CONTENT_TYPES.items():
        if lowered.endswith(suffix):
            return ctype
    return None


def _reject_casefold_collisions(names: Sequence[str]) -> None:
    """Refuse ``A.js`` alongside ``a.js``.

    On a case-insensitive volume -- which is the default on Windows and an
    option on macOS -- those are ONE file and TWO index entries, so the second
    write silently overwrites the first and the mod ships bytes nobody
    reviewed under a name the manifest still lists."""
    seen: Dict[str, str] = {}
    for name in names:
        folded = name.casefold()
        if folded in seen:
            raise ValidationError(
                "bad_file_name",
                f"{name!r} collides with {seen[folded]!r} when case is folded")
        seen[folded] = name


# ---- CSS: no external origin ----------------------------------------------

#: ``@import`` is a literal plus a word boundary -- no quantifier, so it scans
#: linearly. The comment/string walk below is hand-written for the same reason:
#: the obvious ``/\*.*?\*/`` and ``url\((.*?)\)`` are QUADRATIC on input that
#: opens thousands of comments (or ``url(``) and never closes them, and this
#: runs ON THE EVENT LOOP against up to 256 KiB of attacker-chosen text. One
#: installed stylesheet could otherwise stall every terminal on the broker.
_CSS_IMPORT_RE = re.compile(r"@import\b", re.I)
#: ``//host`` at the start of a URL-ish token: protocol-relative, so it inherits
#: the page's scheme and reaches an external origin just as ``https://`` does.
_CSS_PROTOCOL_RELATIVE_RE = re.compile(r"\A\s*(?:[a-z][a-z0-9+.\-]*:)?//", re.I)


def _strip_css_comments(text: str) -> Tuple[str, List[str]]:
    """One linear, STRING-AWARE pass. Returns ``(text with comments blanked,
    every quoted string literal found outside a comment)``.

    String awareness is load-bearing, not pedantry: ``content: "/*"`` is a
    perfectly ordinary declaration, and a comment stripper that does not know
    about strings treats it as an unterminated comment and silently drops the
    REST OF THE FILE -- so the external ``url()`` on the next line is never
    seen. An unterminated comment that really is one runs to end of file, which
    is what a browser does."""
    out: List[str] = []
    strings: List[str] = []
    pos, length = 0, len(text)
    while pos < length:
        ch = text[pos]
        if ch == "/" and text.startswith("/*", pos):
            end = text.find("*/", pos + 2)
            out.append(" ")
            if end < 0:
                break                      # unterminated: comment to EOF
            pos = end + 2
            continue
        if ch in "\"'":
            end = pos + 1
            while end < length:
                if text[end] == "\\":
                    end += 2
                    continue
                if text[end] == ch or text[end] == "\n":
                    break
                end += 1
            strings.append(text[pos + 1:min(end, length)])
            out.append(text[pos:min(end + 1, length)])
            pos = end + 1
            continue
        out.append(ch)
        pos += 1
    return "".join(out), strings


def _css_url_values(text: str) -> List[str]:
    """Every ``url(…)`` argument, unquoted and trimmed. One linear pass."""
    lowered = text.casefold()
    out: List[str] = []
    pos = 0
    while True:
        start = lowered.find("url(", pos)
        if start < 0:
            return out
        end = text.find(")", start + 4)
        if end < 0:
            return out
        value = text[start + 4:end].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].strip()
        out.append(value)
        pos = end + 1


def _reject_css_external_references(name: str, text: str) -> None:
    """Refuse ``@import`` and any reference to an external origin.

    The app's CSP sets ONLY ``script-src`` and ``frame-ancestors`` -- there is
    no ``default-src`` and no ``style-src`` -- so a stylesheet's ``@import``,
    ``url()`` and ``@font-face`` are entirely unrestricted, and would be a
    SILENT egress channel out of a broker whose only outbound HTTP is the
    deliberately-closed ``/status/fetch``.

    The rule is deliberately BROADER than "no absolute url()": an installed
    stylesheet may not contain ``http://`` or ``https://`` outside a comment AT
    ALL, and no ``url()`` argument or string literal may start with ``//``.
    Enumerating URL-bearing syntax instead (``url()``, ``@import``,
    ``image-set()``, ``-webkit-image-set()``, ``src()``, whatever CSS adds next)
    is a list that is wrong the moment it is written -- ``image-set("https://…"
    1x)`` contains no ``url()`` at all and would sail through. One coarse rule
    is enforceable, statable in a sentence, and has an obvious workaround
    (``data:`` or a relative path).

    DEFENCE IN DEPTH, NOT A BOUNDARY: the mod's own JS can ``fetch()`` anything,
    and #163 settled that ``ctx`` is not a boundary, so a determined author
    evades this with a CSS escape (``@\\69mport``) or a runtime-built
    stylesheet. What it buys is that the silent channel is not the ACCIDENTAL
    one -- it preserves the "no third-party origin loads in this page" property
    #143/#146 bought, and an author who trips it learns the rule."""
    stripped, strings = _strip_css_comments(text)
    if _CSS_IMPORT_RE.search(stripped):
        raise ValidationError("css_external_reference",
                              f"{name}: @import may not be used")
    lowered = stripped.casefold()
    for scheme in ("http://", "https://"):
        found = lowered.find(scheme)
        if found >= 0:
            raise ValidationError(
                "css_external_reference",
                f"{name}: {stripped[found:found + 64]} names an external "
                f"origin (use a relative path or a data: URI)")
    for value in _css_url_values(stripped) + strings:
        if _CSS_PROTOCOL_RELATIVE_RE.match(value):
            raise ValidationError(
                "css_external_reference",
                f"{name}: {value[:64]} is protocol-relative, so it names an "
                f"external origin")


# ---- per-file byte rules ---------------------------------------------------

def count_lines(text: str) -> int:
    """Lines by ECMAScript's definition, not by ``\\n`` alone.

    JavaScript ends a line on LF, CR, U+2028 and U+2029, and treats CRLF as one
    -- so a file of 100k CR-separated lines with a single trailing LF counts as
    ONE line to a naive ``text.count("\\n")`` and walks straight through a cap
    whose whole purpose is "no fragment is a 100k-line wall of code"."""
    return (text.count("\n") + text.count("\r") - text.count("\r\n")
            + text.count("\u2028") + text.count("\u2029"))


def _file_bytes(name: str, text: Any) -> bytes:
    """The UTF-8 bytes of one file, or a ``ValidationError``.

    The same rules ``ui._css_servable`` applies to a spliced fragment: real
    text, no BOM, ends in its own newline, valid UTF-8, and under the shared
    line cap. A lone surrogate is caught here as ``UnicodeEncodeError`` and
    answered ``bad_encoding`` -- never allowed to become a 500 three layers
    down."""
    if not isinstance(text, str):
        raise ValidationError("bad_encoding", f"{name}: not a string")
    if text.startswith("﻿"):
        raise ValidationError("bad_encoding", f"{name}: carries a UTF-8 BOM")
    if not text.endswith("\n"):
        raise ValidationError("bad_encoding",
                              f"{name}: does not end in a newline")
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("bad_encoding", f"{name}: {exc}") from None
    if len(data) > MAX_FILE_BYTES:
        raise ValidationError("file_too_large",
                              f"{name}: {len(data)} > {MAX_FILE_BYTES} bytes")
    cap = line_cap()
    if count_lines(text) > cap:
        raise ValidationError("file_too_large",
                              f"{name}: over {cap} lines")
    return data


# ---- the manifest ----------------------------------------------------------

#: Every key a manifest may carry. UNKNOWN KEYS ARE REJECTED, not ignored, so a
#: typo ("styles" -> "style") is loud at install time instead of a mod that
#: silently ships without its stylesheet.
MANIFEST_KEYS = {"id", "version", "ctxVersion", "title", "description",
                 "scripts", "styles", "requires", "tiers", "help",
                 "defaultEnabled"}
HELP_KEYS = {"label", "icon", "order", "slug"}


def _text_field(meta: Dict[str, Any], key: str, cap: int) -> str:
    value = meta.get(key, "")
    if not isinstance(value, str) or len(value) > cap:
        raise ValidationError("bad_manifest_field",
                              f"{key} must be a string of at most {cap} chars")
    return value


def _name_list(meta: Dict[str, Any], key: str, suffix: str,
               files: Dict[str, Any], code: str) -> List[str]:
    """A manifest list of bare filenames, each grammar-valid, of the right
    kind, present in ``files``, and declared at most once."""
    raw = meta.get(key, [])
    if not isinstance(raw, list) or len(raw) > MAX_FILES:
        raise ValidationError(code, f"{key} must be a list of <= {MAX_FILES}")
    out: List[str] = []
    for entry in raw:
        if file_name_error(entry) is not None:
            raise ValidationError(code, f"{key}: bad file name {entry!r}")
        if not entry.casefold().endswith(suffix):
            raise ValidationError(code, f"{key}: {entry!r} is not a {suffix}")
        if entry in out:
            raise ValidationError(code, f"{key}: {entry!r} declared twice")
        if entry not in files:
            raise ValidationError(code, f"{key}: {entry!r} is not in files")
        out.append(entry)
    return out


def _canonical_manifest(meta: Any, files: Dict[str, Any]) -> Dict[str, Any]:
    """Every field typed, capped and normalized, or a ``ValidationError``.

    The RESULT is what the broker writes to ``mod.json`` and what feeds the
    generation hash -- the payload's own bytes are never persisted -- so an
    installed mod's manifest always says exactly what was validated.

    ``defaultEnabled`` is accepted and then DROPPED: an installed mod is always
    reported ``default_enabled: false``, whatever it declares. Installing on
    one broker must not silently switch a mod on for every browser that loads
    its page; the deliberate way to do that is a #157 pin. Install is two steps
    -- install, then enable -- which is also how the shipped default-off mods
    already behave, and it bounds the blast radius of a bad mod."""
    from .app import _MODSTORE_ID_RE, _is_reserved_mod_id

    if not isinstance(meta, dict):
        raise ValidationError("bad_json", "manifest must be an object")
    unknown = sorted(set(meta) - MANIFEST_KEYS)
    if unknown:
        raise ValidationError("unknown_manifest_key",
                              f"unknown manifest key(s): {', '.join(unknown)}")

    mod_id = meta.get("id")
    if not isinstance(mod_id, str) or not _MODSTORE_ID_RE.fullmatch(mod_id):
        raise ValidationError("bad_mod_id", f"bad mod id {mod_id!r}")
    if _is_reserved_mod_id(mod_id):
        raise ValidationError(
            "reserved_id",
            f"{mod_id!r} is in the first-party namespace; an installed mod id "
            f"must start with 'x-' (see #172)")

    ctx_version = meta.get("ctxVersion", 1)
    if not isinstance(ctx_version, int) or isinstance(ctx_version, bool):
        raise ValidationError("bad_manifest_field", "ctxVersion must be an int")

    scripts_raw = meta.get("scripts")
    if not isinstance(scripts_raw, list) or not scripts_raw:
        raise ValidationError(
            "bad_scripts",
            "scripts is required and must be a non-empty ordered list of .js "
            "names (the shipped tree's legacy `entry` field is not accepted)")
    scripts = _name_list(meta, "scripts", ".js", files, "bad_scripts")
    styles = _name_list(meta, "styles", ".css", files, "bad_styles")

    requires_raw = meta.get("requires", [])
    if not isinstance(requires_raw, list) or len(requires_raw) > MAX_REQUIRES:
        raise ValidationError("bad_requires",
                              f"requires must be a list of <= {MAX_REQUIRES}")
    requires: List[str] = []
    for dep in requires_raw:
        if not isinstance(dep, str) or not _MODSTORE_ID_RE.fullmatch(dep):
            raise ValidationError("bad_requires", f"bad required id {dep!r}")
        if dep == mod_id:
            raise ValidationError("bad_requires", "a mod cannot require itself")
        if dep not in requires:
            requires.append(dep)

    tiers_raw = meta.get("tiers", [])
    if not isinstance(tiers_raw, list) or len(tiers_raw) > MAX_TIERS:
        raise ValidationError("bad_manifest_field",
                              f"tiers must be a list of <= {MAX_TIERS}")
    tiers: List[str] = []
    for tier in tiers_raw:
        if not isinstance(tier, str) or not tier or len(tier) > MAX_TIER_LEN:
            raise ValidationError("bad_manifest_field", f"bad tier {tier!r}")
        tiers.append(tier)

    help_raw = meta.get("help", {})
    if not isinstance(help_raw, dict):
        raise ValidationError("bad_manifest_field", "help must be an object")
    help_unknown = sorted(set(help_raw) - HELP_KEYS)
    if help_unknown:
        raise ValidationError("unknown_manifest_key",
                              f"unknown help key(s): {', '.join(help_unknown)}")
    help_block: Dict[str, Any] = {}
    label = help_raw.get("label")
    if label is not None:
        if not isinstance(label, str) or len(label) > MAX_TITLE_LEN:
            raise ValidationError("bad_manifest_field", "help.label")
        help_block["label"] = label
    icon = help_raw.get("icon")
    if icon is not None:
        if not isinstance(icon, str) or len(icon) > MAX_ICON_LEN:
            raise ValidationError("bad_manifest_field", "help.icon")
        help_block["icon"] = icon
    order = help_raw.get("order")
    if order is not None:
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValidationError("bad_manifest_field", "help.order")
        help_block["order"] = order
    # help.slug is accepted and DROPPED: an installed section's slug is forced
    # to the mod id, so a collision with a wiki or shipped slug is impossible
    # rather than merely handled (see help_corpus.merge_installed_sections).

    default_enabled = meta.get("defaultEnabled")
    if default_enabled is not None and not isinstance(default_enabled, bool):
        raise ValidationError("bad_manifest_field",
                              "defaultEnabled must be a bool")

    out = {"id": mod_id,
           "version": _text_field(meta, "version", MAX_VERSION_LEN),
           "ctxVersion": ctx_version,
           "title": _text_field(meta, "title", MAX_TITLE_LEN) or mod_id,
           "description": _text_field(meta, "description",
                                      MAX_DESCRIPTION_LEN),
           "scripts": scripts,
           "styles": styles,
           "requires": requires,
           "tiers": tiers,
           "help": help_block}
    # ONE encodability check over the whole result, because every string in it
    # has to survive both the generation hash and the mod.json write. JSON can
    # carry a LONE SURROGATE ("\ud800"), Python happily holds it as a str, and
    # it only explodes at .encode("utf-8") -- which happens in compute_gen,
    # OUTSIDE the caller's validation try/except. That would be a 500 for a
    # malformed payload, and worse, the same manifest on disk would then throw
    # during a scan and take out a startup or a rescan.
    try:
        json.dumps(out, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(
            "bad_encoding", f"manifest carries unencodable text: {exc}") from None
    return out


def validate_package(manifest: Any, files: Any) -> Tuple[Dict[str, Any],
                                                         Dict[str, Dict]]:
    """Validate one mod package ENTIRELY IN MEMORY.

    Returns ``(canonical_manifest, records)`` where each record is
    ``{"data": bytes, "sha256": hex, "integrity": "sha256-<b64>"}``. Raises
    ``ValidationError`` on the first problem. Not a single byte is written by
    anything here, so a refusal leaves the store untouched by construction
    rather than by careful unwinding.

    The SAME function validates an install payload and a directory the scanner
    read off disk -- there is exactly one rule set, so a hand-populated mod can
    never be served under weaker rules than an installed one."""
    if not isinstance(files, dict):
        raise ValidationError("bad_json", "files must be an object")
    if len(files) > MAX_FILES:
        raise ValidationError("too_many_files",
                              f"{len(files)} files > {MAX_FILES}")
    for name in files:
        code = file_name_error(name)
        if code is not None:
            raise ValidationError(code, f"bad file name {name!r}")
    _reject_casefold_collisions(list(files))

    records: Dict[str, Dict[str, Any]] = {}
    total = 0
    for name in sorted(files):
        data = _file_bytes(name, files[name])
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValidationError("total_too_large",
                                  f"package over {MAX_TOTAL_BYTES} bytes")
        if name.casefold().endswith(".css"):
            _reject_css_external_references(name, files[name])
        digest = hashlib.sha256(data).digest()
        records[name] = {
            "data": data,
            "sha256": digest.hex(),
            "integrity": "sha256-" + base64.b64encode(digest).decode("ascii"),
        }
    return _canonical_manifest(manifest, files), records


def compute_gen(manifest: Dict[str, Any],
                records: Dict[str, Dict[str, Any]]) -> str:
    """The generation id: sha256 over the CANONICAL manifest bytes plus the
    sorted ``(name, sha256)`` pairs.

    Covering the manifest matters -- a mod whose only change is its
    ``requires`` list has identical files but is a different generation, and
    reusing the old ``gen`` would leave every cached URL pointing at the old
    dependency graph."""
    hasher = hashlib.sha256()
    hasher.update(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8"))
    hasher.update(b"\n")
    for name in sorted(records):
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(records[name]["sha256"].encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


# ---- the in-memory index ---------------------------------------------------

def empty_index() -> Dict[str, Any]:
    """The index a broker with no installed mods (or no ``mods_dir``) holds.

    ``mods``    id -> record (the CURRENT generation only -- what the catalog
                and Help are built from);
    ``assets``  "<id>/<gen>/<name>" -> (bytes, content_type), covering the
                current AND the retained previous generation, so a page that
                started booting before a replace can still fetch its files;
    ``skipped`` directory name -> refusal code, for the operator-facing
                ``GET /mods/installed`` (a mod that silently vanishes is worse
                than one that says why)."""
    return {"mods": {}, "assets": {}, "skipped": {}}


def assets_for(mod_id: str, gen: str,
               records: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple]:
    """The allowlist entries one generation contributes. Only ``.js``/``.css``:
    ``help.md`` and the broker-written metadata are inputs, never URLs."""
    out: Dict[str, Tuple[bytes, str]] = {}
    for name, rec in records.items():
        ctype = content_type(name)
        if ctype is not None:
            out[f"{mod_id}/{gen}/{name}"] = (rec["data"], ctype)
    return out


def make_record(manifest: Dict[str, Any], records: Dict[str, Dict[str, Any]],
                gen: str, installed_at: int,
                previous: Optional[str] = None) -> Dict[str, Any]:
    """One ``index["mods"]`` entry. ``help_md`` is captured HERE, at scan or
    install time, so Help is built from the very bytes being served instead of
    from a second traversal that could disagree.

    ``previous`` is the retained predecessor generation (or ``None``), carried
    in memory as well as in CURRENT so the GC and the sweep never have to guess
    which other directory is still live."""
    # Case-folded, because "Help.md" passes the grammar and would otherwise be
    # captured as a servable-nothing file whose help silently never appears.
    help_rec = next((rec for name, rec in records.items()
                     if name.casefold() == HELP_NAME), None)
    return {
        "id": manifest["id"],
        "gen": gen,
        "previous": previous,
        "manifest": manifest,
        "installed_at": installed_at,
        "files": {name: {"sha256": rec["sha256"],
                         "integrity": rec["integrity"],
                         "bytes": len(rec["data"])}
                  for name, rec in records.items()},
        "help_md": (help_rec["data"].decode("utf-8")
                    if help_rec is not None else None),
    }


# ---- reading the store off disk -------------------------------------------

def _lstat_is_link_or_reparse(path: str) -> bool:
    """True for a symlink, a Windows junction, or anything else carrying a
    reparse point -- and for anything unreadable, because an entry we cannot
    classify must not be walked. ``os.path.islink`` alone is not enough on
    Windows: a directory junction is a reparse point that it reports as a
    plain directory before 3.12."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _within(root_real: str, path: str) -> bool:
    """True iff ``path``'s realpath is ``root_real`` or below it. ``normcase``
    on both sides because the comparison has to hold on a case-insensitive
    volume too."""
    try:
        real = os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return False
    root = os.path.normcase(root_real)
    return real == root or real.startswith(root + os.sep)


def _read_json_capped(path: Path, cap: int = MAX_META_BYTES) -> Any:
    """Read at most ``cap`` bytes and parse them as JSON, or ``None``.

    Capped BEFORE the parse and never with ``read_bytes()``: a scan must not be
    able to pull an arbitrarily large file into memory (a 10 GB ``mod.json``
    dropped in ``mods_dir`` would otherwise OOM a startup), and it must not be
    able to raise -- a hand-mangled sidecar degrades to "that mod is not
    served", never to a broker that will not boot. ``RecursionError`` is caught
    too: it is what a deeply nested JSON value raises, and it is not an
    ``Exception`` subclass most callers remember to name."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
        if len(raw) > cap:
            LOGGER.warning("installed mod metadata over %d bytes (%s)",
                           cap, path)
            return None
        return json.loads(raw.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError,
            json.JSONDecodeError, ValueError, RecursionError):
        return None


def read_current(mod_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """``(gen, previous)`` from ``<mod>/CURRENT``, or ``(None, None)``.

    The pointer IS the commit, so this is deliberately strict: anything that is
    not a JSON object carrying a well-formed sha256 hex ``gen`` means "this mod
    has no live generation", which is the same answer a crash mid-install
    leaves behind.

    ``previous`` names the ONE retained predecessor. It is read from here and
    never inferred from directory mtimes -- see CURRENT_NAME for why guessing
    loses data. A pointer without it (hand-written, or from a broker predating
    this) simply retains nothing, which is safe: the live generation is intact
    and only a page mid-boot across a replace would have wanted the other."""
    data = _read_json_capped(mod_path / CURRENT_NAME)
    if not isinstance(data, dict):
        return None, None
    gen = data.get("gen")
    if not isinstance(gen, str) or not GEN_RE.fullmatch(gen):
        return None, None
    previous = data.get("previous")
    if not isinstance(previous, str) or not GEN_RE.fullmatch(previous) \
            or previous == gen:
        previous = None
    return gen, previous


def _read_generation(gen_path: Path, root_real: str
                     ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], int]:
    """Read + validate one ``<id>/<gen>/`` directory. Raises ``ValidationError``.

    Files whose names fail the grammar, or whose suffix we do not ship, are
    IGNORED with a log rather than refusing the mod: a stray ``.DS_Store`` or a
    leftover editor backup is inert junk, and refusing a whole mod over it is
    hostile. A file the manifest actually DECLARES is a different matter -- it
    will not be in the map, and the declaration check refuses the mod.

    THE DIRECTORY NAME MUST CONTENT-ADDRESS ITS OWN BYTES. A mismatch is fatal,
    not a warning, because ``gen`` is in the URL and that URL is served
    ``immutable`` for a year with an SRI hash derived from these very bytes.
    Tolerating a mismatch means editing a file in place silently republishes
    different content behind an identical, already-cached URL -- browsers
    holding the old copy then fail SRI and the mod does not load at all. It also
    means "the same gen" stops implying "the same bytes", which is the one
    property the whole generation scheme exists to provide."""
    if _lstat_is_link_or_reparse(str(gen_path)) or not _within(root_real,
                                                              str(gen_path)):
        raise ValidationError("bad_mod_id", f"{gen_path}: not a plain directory")
    files: Dict[str, str] = {}
    try:
        entries = list(os.scandir(gen_path))
    except OSError as exc:
        raise ValidationError("bad_encoding", f"{gen_path}: {exc}") from None
    for entry in entries:
        if entry.name in (MANIFEST_NAME, GEN_META_NAME):
            continue
        if file_name_error(entry.name) is not None:
            LOGGER.debug("installed mod %s: ignoring %r", gen_path, entry.name)
            continue
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            LOGGER.warning("installed mod %s: ignoring non-file %r",
                           gen_path, entry.name)
            continue
        if len(files) >= MAX_FILES:
            raise ValidationError("too_many_files", str(gen_path))
        # Capped READ, not a read-then-check: read_bytes() on a 10 GB file
        # dropped in mods_dir would OOM the scan before the cap ever ran.
        try:
            with open(gen_path / entry.name, "rb") as fh:
                raw = fh.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise ValidationError("bad_encoding",
                                  f"{entry.name}: {exc}") from None
        if len(raw) > MAX_FILE_BYTES:
            raise ValidationError("file_too_large", entry.name)
        try:
            files[entry.name] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("bad_encoding",
                                  f"{entry.name}: {exc}") from None
    manifest_raw = _read_json_capped(gen_path / MANIFEST_NAME)
    if manifest_raw is None:
        raise ValidationError("bad_json",
                              f"{gen_path}/{MANIFEST_NAME} unreadable")
    manifest, records = validate_package(manifest_raw, files)
    computed = compute_gen(manifest, records)
    if computed != gen_path.name:
        raise ValidationError(
            "bad_generation",
            f"{gen_path.name[:12]} does not content-address its own bytes "
            f"(they hash to {computed[:12]}) -- install through POST "
            f"/mods/install rather than writing mods_dir by hand")
    return manifest, records, _read_installed_at(gen_path)


def _read_installed_at(gen_path: Path) -> int:
    """``.gen.json``'s ``installed_at``, else the directory mtime.

    The sidecar is why the stamp survives a restore, a copy or a rescan -- a
    filesystem mtime does not. The mtime is only the fallback for a
    hand-populated generation, where there is no better answer and the stamp is
    honestly unstable."""
    meta = _read_json_capped(gen_path / GEN_META_NAME)
    stamp = meta.get("installed_at") if isinstance(meta, dict) else None
    if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0:
        return stamp
    try:
        return int(gen_path.stat().st_mtime)
    except OSError:
        return 0


def scan(mods_dir: Optional[Path]) -> Dict[str, Any]:
    """Read the whole store into a fresh index. BLOCKING.

    Never raises: a broker whose ``mods_dir`` is missing, unreadable or full of
    junk must still boot, exactly as a corrupt ``webterm_state.json`` degrades
    to an empty state rather than blocking startup. Every refusal is recorded
    in ``index["skipped"]`` and logged."""
    index = empty_index()
    if mods_dir is None:
        return index
    mods_dir = Path(mods_dir)
    if not mods_dir.is_dir():
        return index
    try:
        root_real = os.path.normcase(os.path.realpath(str(mods_dir)))
    except (OSError, ValueError):
        return index
    try:
        entries = sorted(os.scandir(mods_dir), key=lambda e: e.name)
    except OSError as exc:
        LOGGER.warning("mods_dir unreadable (%s): %s", mods_dir, exc)
        return index

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue                          # marker + .tmp-/.old- litter
        if not entry.is_dir(follow_symlinks=False):
            # A SYMLINK to a directory lands here, because is_dir(follow_
            # symlinks=False) is False for a link -- so classify before
            # dropping: a link is a refusal the operator should see, a loose
            # file is just litter. (A Windows JUNCTION is the other way round:
            # it reports as a plain directory here and is caught by the reparse
            # check in _scan_one.)
            if _lstat_is_link_or_reparse(str(mods_dir / name)):
                LOGGER.warning("mods_dir: skipping %r -- symlink or reparse "
                               "point", name)
                index["skipped"][name] = "bad_mod_id"
            continue
        if len(index["mods"]) >= MAX_MODS:
            LOGGER.warning("mods_dir holds more than %d mods; ignoring %r",
                           MAX_MODS, name)
            index["skipped"][name] = "too_many_mods"
            continue
        code = _scan_one(mods_dir, name, root_real, index)
        if code is not None:
            index["skipped"][name] = code
    return index


def _scan_one(mods_dir: Path, name: str, root_real: str,
              index: Dict[str, Any]) -> Optional[str]:
    """Fold one ``<mods_dir>/<name>/`` into ``index``; return a refusal code."""
    from .app import _MODSTORE_ID_RE, _is_reserved_mod_id

    mod_path = mods_dir / name
    if not _MODSTORE_ID_RE.fullmatch(name):
        LOGGER.warning("mods_dir: %r is not a mod-id-shaped directory", name)
        return "bad_mod_id"
    if _is_reserved_mod_id(name):
        # #172: a hand-dropped mods_dir/clock/ must NEVER shadow the shipped
        # clock -- it would inherit its pins, its /mod-store value and its
        # webterm:mod:clock:* keys. Loud, because it is always a mistake.
        LOGGER.warning("mods_dir: skipping %r -- an installed mod id must "
                       "start with 'x-' (the unprefixed namespace is reserved "
                       "for shipped mods, see #172)", name)
        return "reserved_id"
    if _lstat_is_link_or_reparse(str(mod_path)):
        LOGGER.warning("mods_dir: skipping %r -- symlink or reparse point", name)
        return "bad_mod_id"
    if not _within(root_real, str(mod_path)):
        LOGGER.warning("mods_dir: skipping %r -- resolves outside mods_dir",
                       name)
        return "bad_mod_id"
    gen, previous = read_current(mod_path)
    if gen is None:
        LOGGER.warning("mods_dir: skipping %r -- no usable %s pointer",
                       name, CURRENT_NAME)
        return "not_installed"
    try:
        manifest, records, installed_at = _read_generation(mod_path / gen,
                                                           root_real)
    except ValidationError as exc:
        LOGGER.warning("mods_dir: skipping %r -- %s: %s", name, exc.code, exc)
        return exc.code
    if manifest["id"] != name:
        LOGGER.warning("mods_dir: skipping %r -- its manifest claims id %r",
                       name, manifest["id"])
        return "bad_mod_id"
    retained = _load_retained_generation(mod_path, name, previous, root_real,
                                         index)
    index["mods"][name] = make_record(manifest, records, gen, installed_at,
                                      retained)
    index["assets"].update(assets_for(name, gen, records))
    return None


def _load_retained_generation(mod_path: Path, mod_id: str,
                              previous: Optional[str], root_real: str,
                              index: Dict[str, Any]) -> Optional[str]:
    """Also serve the retained PREVIOUS generation's assets, if CURRENT names
    one. Returns the generation actually loaded, or ``None``.

    A page that started booting against generation A must not be handed a file
    from generation B, and a broker restart in that window is the one case the
    install path's in-memory carry-over cannot cover. Assets only -- the
    catalog, Help and the policy all describe CURRENT and nothing else.

    The predecessor is READ FROM CURRENT, never chosen by mtime. Guessing loses
    data twice over: an install that died between the generation rename and the
    commit leaves a NEWER directory that was never live and would displace the
    real predecessor (which the next sweep then deletes), and a predecessor
    whose newer neighbour merely fails validation would never be reached at all.

    A predecessor that no longer validates is dropped without noise: it is not
    the live generation, so nothing depends on it being there."""
    if previous is None:
        return None
    gen_path = mod_path / previous
    if not gen_path.is_dir():
        return None
    try:
        _, records, _ = _read_generation(gen_path, root_real)
    except (ValidationError, OSError):
        LOGGER.debug("mods_dir: %s retained generation %s is unusable",
                     mod_id, previous[:12])
        return None
    index["assets"].update(assets_for(mod_id, previous, records))
    return previous


# ---- writing: the commit protocol -----------------------------------------
#
# 1. validate the payload IN MEMORY and compute the content-addressed `gen`
#    (done by the caller, before the lock -- a pure fast fail);
# 2. re-check the STATE-DEPENDENT conditions (id_in_use, too_many_mods) UNDER
#    the lock, because the pre-lock check is only a fast fail and two requests
#    can otherwise both observe the id absent;
# 3. write `<id>/.tmp-<rand>/` and rename it to `<id>/<gen>/` -- a FRESH name,
#    so this step can never destroy anything;
# 4. `_write_state_atomic(<id>/CURRENT, {"gen": …, "previous": …})` -- THIS IS
#    THE COMMIT;
# 5. rebuild the in-memory index from the bytes just written (never re-read
#    from disk) -- a pure function, so it cannot fail on IO;
# 6. GC to RETAINED_GENERATIONS, best-effort.
#
# Crash recovery is total and unambiguous: CURRENT names a complete generation
# (the old one or the new one) or does not exist. If step 4 never runs, the old
# CURRENT is still live and step 3's directory is litter. Nothing here ever
# renames a directory OVER another, which is what the earlier "two renames"
# shape got wrong -- it lost the last good copy on a crash in between.
#
# SCOPE OF THE GUARANTEE: this is PROCESS-crash safe, not power-loss durable.
# Nothing here fsyncs, exactly like `_write_bytes_atomic` and every other write
# in this broker ("visibility only -- crash durability is deliberately not
# promised"). After a host power cut NTFS may have recovered the CURRENT replace
# while the generation's contents are still missing or short; the scan then
# refuses that generation and reports it, rather than serving truncated code.

def _has_marker(mods_dir: Path) -> bool:
    return (Path(mods_dir) / MARKER_NAME).is_file()


def ensure_store(mods_dir: Path) -> None:
    """Create ``mods_dir`` and drop the ownership marker. BLOCKING.

    The marker is what the destructive sweep refuses to run without, so a
    ``mods_dir`` mis-set to an existing user directory cannot have unrelated
    trees deleted out from under it. Writing it on FIRST USE (an install)
    rather than at startup means a mis-set path stays un-marked until somebody
    deliberately installs into it."""
    mods_dir = Path(mods_dir)
    mods_dir.mkdir(parents=True, exist_ok=True)
    marker = mods_dir / MARKER_NAME
    if not marker.exists():
        marker.write_text(
            "This directory holds Browserland's runtime-installed mods.\n"
            "The broker will delete generation directories and .tmp-/.old-\n"
            "staging inside it. Do not point mods_dir at a directory you care\n"
            "about; deleting this marker disables the sweep.\n", encoding="utf-8")


def _generation_payload(manifest: Dict[str, Any],
                        records: Dict[str, Dict[str, Any]],
                        gen: str, installed_at: int) -> Dict[str, bytes]:
    """Every byte one generation directory holds, name -> bytes.

    ``mod.json`` is BROKER-WRITTEN from the canonical manifest (and is a
    reserved key in an install payload), so an installed mod's manifest always
    says exactly what was validated -- a payload cannot ship one that
    disagrees. ``.gen.json`` carries the stamp and the per-file hashes."""
    out = {name: rec["data"] for name, rec in records.items()}
    out[MANIFEST_NAME] = (json.dumps(manifest, sort_keys=True, indent=2,
                                     ensure_ascii=False) + "\n").encode("utf-8")
    out[GEN_META_NAME] = (json.dumps(
        {"gen": gen, "installed_at": installed_at,
         "files": {name: rec["sha256"] for name, rec in records.items()}},
        sort_keys=True, indent=2) + "\n").encode("utf-8")
    return out


def _verify_distinct_identities(dir_path: Path, names: Sequence[str]) -> None:
    """Assert the files we just wrote are distinct FILES, not distinct NAMES.

    The grammar already refuses every shape that can collapse two names onto
    one file (case folding, trailing dots, 8.3, alternate data streams), but
    that is a lexical argument about a filesystem whose rules are not fully
    lexical. This is the empirical check, run right after the write and before
    anything is published: two names sharing one ``(st_dev, st_ino)`` means one
    of them silently overwrote the other, and the mod would ship bytes nobody
    reviewed under a name the manifest still lists."""
    seen: Dict[Tuple[int, int], str] = {}
    for name in names:
        try:
            st = os.stat(dir_path / name)
        except OSError as exc:
            raise ValidationError("write_failed", f"{name}: {exc}") from None
        key = (st.st_dev, st.st_ino)
        if st.st_ino and key in seen:
            raise ValidationError(
                "bad_file_name",
                f"{name!r} and {seen[key]!r} are the same file on this volume")
        seen[key] = name


def _write_generation_dir(dir_path: Path, payload: Dict[str, bytes],
                          atomic: bool) -> None:
    """Materialize one generation directory.

    ``atomic=False`` is the staging path: the directory is brand new and
    private, so a plain write is enough and the whole thing is published by
    ONE rename. ``atomic=True`` is the repair path for a target that already
    exists -- the name is content-addressed, so it is either the same bytes or
    the debris of an install that died between the rename and the commit, and
    either way the correct content is what we hold. It is written file by file
    through temp+``os.replace`` and the directory is NEVER removed, because
    CURRENT may already name it.

    The repair is EXACT: anything in the directory that is not in the payload is
    removed too. Overwriting only the expected entries would leave a stale
    ``old.js`` behind -- absent from the in-memory index, but picked up by the
    next scan, which would then be serving bytes that never contributed to this
    generation's hash under that generation's URL."""
    dir_path.mkdir(parents=True, exist_ok=True)
    for name, data in payload.items():
        target = dir_path / name
        if not atomic:
            target.write_bytes(data)
            continue
        tmp = dir_path / f"{TMP_PREFIX}w-{secrets.token_hex(6)}"
        try:
            tmp.write_bytes(data)
            os.replace(str(tmp), str(target))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    if atomic:
        _prune_to_payload(dir_path, payload)
    _verify_distinct_identities(dir_path, list(payload))


def _prune_to_payload(dir_path: Path, payload: Dict[str, bytes]) -> None:
    """Remove every entry of ``dir_path`` that is not in ``payload``.

    Only ever called on a generation directory the broker itself owns, so a
    stray entry is by definition debris. Failures are logged, not raised: the
    content that matters is already correct, and the extra file is at worst
    served under a URL nothing links to."""
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return
    for entry in entries:
        if entry.name in payload:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
        except OSError as exc:
            LOGGER.warning("mods_dir: stale %s left in %s: %s",
                           entry.name, dir_path, exc)


def commit_generation(mods_dir: Path, mod_id: str, manifest: Dict[str, Any],
                      records: Dict[str, Dict[str, Any]], gen: str,
                      previous: Optional[str] = None) -> int:
    """Steps 3 and 4: stage, publish, commit. Returns ``installed_at``.

    ``previous`` is the generation to retain alongside this one; it is written
    into CURRENT so nothing downstream has to infer it.

    BLOCKING -- call it through ``_off_loop``, inside the shielded region that
    holds ``mods_install_lock``."""
    mods_dir = Path(mods_dir)
    ensure_store(mods_dir)
    mod_path = mods_dir / mod_id
    mod_path.mkdir(parents=True, exist_ok=True)
    installed_at = int(time.time())
    payload = _generation_payload(manifest, records, gen, installed_at)
    target = mod_path / gen

    if target.exists():
        _write_generation_dir(target, payload, atomic=True)
    else:
        staging = mod_path / (TMP_PREFIX + secrets.token_hex(8))
        try:
            _write_generation_dir(staging, payload, atomic=False)
            try:
                os.rename(str(staging), str(target))
            except OSError:
                # Lost a race with something that created the target under us
                # (not possible while the lock is held, but a rename that fails
                # for any other reason must not leave a half-published mod).
                if not target.is_dir():
                    raise
                _write_generation_dir(target, payload, atomic=True)
                shutil.rmtree(staging, ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    # 4. THE COMMIT. One small-file atomic replace; everything before it is
    # invisible, everything after it is bookkeeping.
    from .app import _write_state_atomic
    _write_state_atomic(mod_path / CURRENT_NAME,
                        {"gen": gen, "previous": previous})
    return installed_at


def retained_after(index: Dict[str, Any], mod_id: str,
                   gen: str) -> Optional[str]:
    """Which generation to keep ALONGSIDE ``gen`` when installing it.

    Replacing B with C retains B. Re-installing B over itself (identical bytes,
    so an identical content-addressed name) must retain whatever B was already
    retaining -- treating it as "previous == gen, so retain nothing" would
    delete the real predecessor A and 404 every page mid-boot against it, for
    an install that changed nothing at all."""
    previous = index["mods"].get(mod_id)
    if previous is None:
        return None
    if previous["gen"] != gen:
        return previous["gen"]
    return previous.get("previous")


def index_with(index: Dict[str, Any], mod_id: str, manifest: Dict[str, Any],
               records: Dict[str, Dict[str, Any]], gen: str,
               installed_at: int,
               previous: Optional[str] = None) -> Dict[str, Any]:
    """Step 5: a NEW index carrying the bytes just written. Pure -- no IO, so
    it cannot fail halfway and leave the broker disagreeing with its disk.

    The retained generation's assets are CARRIED OVER, which is the whole point
    of retention: a page that is mid-boot against the old generation keeps
    resolving its URLs instead of getting a 404 for half its files."""
    keep = {gen} | ({previous} if previous else set())
    assets = {key: val for key, val in index["assets"].items()
              if key.split("/", 2)[0] != mod_id or key.split("/", 2)[1] in keep}
    assets.update(assets_for(mod_id, gen, records))
    mods = dict(index["mods"])
    mods[mod_id] = make_record(manifest, records, gen, installed_at, previous)
    skipped = dict(index["skipped"])
    skipped.pop(mod_id, None)
    return {"mods": mods, "assets": assets, "skipped": skipped}


def index_without(index: Dict[str, Any], mod_id: str) -> Dict[str, Any]:
    """The uninstall twin of :func:`index_with`. Pure."""
    return {
        "mods": {mid: rec for mid, rec in index["mods"].items()
                 if mid != mod_id},
        "assets": {key: val for key, val in index["assets"].items()
                   if key.split("/", 2)[0] != mod_id},
        "skipped": {mid: code for mid, code in index["skipped"].items()
                    if mid != mod_id},
    }


def keep_map(index: Dict[str, Any]) -> Dict[str, Set[str]]:
    """mod id -> the generations the index is still serving. What the sweep
    must not delete."""
    out: Dict[str, Set[str]] = {}
    for key in index["assets"]:
        mid, gen, _rest = key.split("/", 2)
        out.setdefault(mid, set()).add(gen)
    for mid, rec in index["mods"].items():
        out.setdefault(mid, set()).add(rec["gen"])
    return out


def _rmtree_if_ours(path: Path) -> None:
    """Remove a directory the BROKER created, refusing to follow a link out of
    the store. Best-effort and silent-on-failure by design: leftover litter is
    a cosmetic problem, a failed sweep that propagates is not."""
    try:
        if _lstat_is_link_or_reparse(str(path)):
            try:
                os.unlink(str(path))
            except OSError:
                try:
                    os.rmdir(str(path))
                except OSError:
                    pass
            return
        shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:
        LOGGER.warning("mods_dir: could not remove %s: %s", path, exc)


def gc_generations(mods_dir: Path, mod_id: str,
                   keep: Sequence[str]) -> None:
    """Step 6: drop everything under ``<id>/`` that is neither a retained
    generation nor the pointer.

    NEVER propagates an IO failure. The commit has already landed by the time
    this runs, so failing the request over litter that could not be reclaimed
    would report a failure for an install that actually succeeded. The litter
    is reclaimed by the next install or rescan instead."""
    mods_dir = Path(mods_dir)
    if not _has_marker(mods_dir):
        LOGGER.warning("mods_dir %s has no %s marker; not sweeping",
                       mods_dir, MARKER_NAME)
        return
    keep_set = set(keep)
    try:
        entries = list(os.scandir(mods_dir / mod_id))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if name in keep_set or name == CURRENT_NAME:
            continue
        if GEN_RE.fullmatch(name) or name.startswith(TMP_PREFIX):
            try:
                _rmtree_if_ours(Path(entry.path))
            except OSError as exc:
                LOGGER.warning("mods_dir: could not reclaim %s: %s",
                               entry.path, exc)


def sweep_store(mods_dir: Path, keep: Dict[str, Set[str]]) -> None:
    """Drop install/uninstall staging and superseded generations across the
    whole store. NEVER raises.

    REFUSES without the ownership marker -- this is the one walk that touches
    the top level, so a ``mods_dir`` mis-set to an existing user directory must
    not be able to lose anything.

    A directory that is not in ``keep`` is LEFT ALONE, deliberately. It is a
    mod the scanner refused (a typo in an id, a mangled manifest, a half-
    unpacked archive), and deleting somebody's tree because we would not serve
    it is the wrong answer -- ``GET /mods/installed`` reports it instead."""
    mods_dir = Path(mods_dir)
    if not _has_marker(mods_dir):
        LOGGER.warning("mods_dir %s has no %s marker; not sweeping",
                       mods_dir, MARKER_NAME)
        return
    try:
        entries = list(os.scandir(mods_dir))
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if name.startswith(TMP_PREFIX) or name.startswith(OLD_PREFIX):
            try:
                _rmtree_if_ours(Path(entry.path))
            except OSError as exc:
                LOGGER.warning("mods_dir: could not reclaim %s: %s",
                               entry.path, exc)
            continue
        if name in keep:
            gc_generations(mods_dir, name, keep[name])


def stage_removal(mods_dir: Path, mod_id: str) -> Optional[str]:
    """Move ``<id>/`` aside to ``.old-<id>-<rand>/`` and return that path.

    The rename IS the uninstall: it is one atomic step, and a crash right after
    it leaves ``.old-`` litter -- which is the intended end state anyway, just
    not yet reclaimed. The caller removes the staged directory afterwards,
    best-effort. Returns ``None`` if there was nothing there."""
    mods_dir = Path(mods_dir)
    mod_path = mods_dir / mod_id
    # lexists, not exists: a DANGLING symlink (which the scanner reports as
    # skipped, so the operator can see it and ask for it to go) is not
    # `exists()`, and answering "nothing here" would report a successful
    # uninstall while leaving the entry on disk for the next scan to resurrect.
    if not os.path.lexists(str(mod_path)):
        return None
    staged = mods_dir / f"{OLD_PREFIX}{mod_id}-{secrets.token_hex(8)}"
    os.rename(str(mod_path), str(staged))
    return str(staged)


def discard_staged(path: str) -> None:
    """Reclaim a directory :func:`stage_removal` moved aside. BLOCKING, and
    best-effort: the uninstall already happened at the rename, so a failure
    here is unreclaimed litter that the next sweep picks up, never a failed
    uninstall."""
    try:
        _rmtree_if_ours(Path(path))
    except OSError as exc:
        LOGGER.warning("mods_dir: could not reclaim %s: %s", path, exc)


def installed_detail(index: Dict[str, Any],
                     mods_dir: Optional[Path]) -> Dict[str, Any]:
    """The token-gated operator view (``GET /mods/installed``).

    Everything ``/info`` deliberately leaves out -- per-file sizes and hashes,
    ``installed_at``, the tiers a mod declares, and WHAT WAS SKIPPED AND WHY.
    /info is fetched by every peer, so it stays small; this is fetched by one
    Control Panel and can afford to be complete. A mod that silently vanishes
    is worse than one that says why it was refused."""
    mods = []
    for mid in sorted(index["mods"]):
        rec = index["mods"][mid]
        meta = rec["manifest"]
        mods.append({
            "id": mid,
            "gen": rec["gen"],
            "title": meta.get("title", mid),
            "description": meta.get("description", ""),
            "version": meta.get("version", ""),
            "ctx_version": meta.get("ctxVersion"),
            "installed_at": rec["installed_at"],
            "requires": list(meta.get("requires", [])),
            "tiers": list(meta.get("tiers", [])),
            "scripts": list(meta.get("scripts", [])),
            "styles": list(meta.get("styles", [])),
            "has_help": rec["help_md"] is not None,
            "files": [{"name": name, "bytes": meta_f["bytes"],
                       "sha256": meta_f["sha256"]}
                      for name, meta_f in sorted(rec["files"].items())],
            "total_bytes": sum(f["bytes"] for f in rec["files"].values()),
        })
    return {
        "mods_dir": str(mods_dir) if mods_dir is not None else None,
        "mods": mods,
        "skipped": dict(index["skipped"]),
        "limits": {"max_mods": MAX_MODS, "max_files": MAX_FILES,
                   "max_file_bytes": MAX_FILE_BYTES,
                   "max_total_bytes": MAX_TOTAL_BYTES,
                   "max_body_bytes": MAX_BODY_BYTES,
                   "max_lines": line_cap(),
                   "retained_generations": RETAINED_GENERATIONS},
    }


# ---- the catalog -----------------------------------------------------------

def catalog(index: Dict[str, Any],
            shipped_ids: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """The installed half of ``/info``'s ``mods``, topologically sorted.

    Shipped rows are emitted first by the caller and a shipped ``requires`` may
    never name an ``x-`` id (CI-guarded), so shipped->installed edges cannot
    exist and only installed->installed edges drive the sort. An edge to an id
    that is in NEITHER set is DROPPED from the sort and reported in
    ``missing_requires`` -- it must not contribute an indegree, or an installed
    mod requiring a shipped mod would come out marked cyclic.

    Kahn's residual is NOT the cycle set: for ``A->B, B->A, C->A`` it is
    ``{A,B,C}``, but ``C`` is merely blocked BY a cycle. So the residual is
    split with Tarjan into ``requires_cycle`` (an SCC of size > 1, or a
    self-loop) and ``blocked_by_cycle``, which the loader renders as distinct
    statuses.

    ``default_enabled`` is unconditionally ``False`` for an installed mod --
    see ``_canonical_manifest``."""
    records = index.get("mods", {})
    ids = sorted(records)
    known = set(ids) | set(shipped_ids)

    deps: Dict[str, List[str]] = {}
    missing: Dict[str, List[str]] = {}
    for mid in ids:
        edges, absent = [], []
        for dep in records[mid]["manifest"].get("requires", []):
            if dep in records and dep != mid:
                if dep not in edges:
                    edges.append(dep)
            elif dep not in known:
                absent.append(dep)
        deps[mid] = edges
        missing[mid] = absent

    order = _kahn(ids, deps)
    residual = [mid for mid in ids if mid not in set(order)]
    in_cycle = _cycle_members(residual, deps)

    rows = [_row(records[mid], deps, missing[mid], None) for mid in order]
    for mid in residual:
        rows.append(_row(records[mid], deps, missing[mid],
                         "requires_cycle" if mid in in_cycle
                         else "blocked_by_cycle"))
    return rows


def _kahn(ids: List[str], deps: Dict[str, List[str]]) -> List[str]:
    """Dependency-first order. Ties broken by the (sorted) id order, so the
    result is stable across restarts and two brokers agree."""
    indegree = {mid: len(deps[mid]) for mid in ids}
    dependents: Dict[str, List[str]] = {mid: [] for mid in ids}
    for mid in ids:
        for dep in deps[mid]:
            dependents[dep].append(mid)
    ready = [mid for mid in ids if indegree[mid] == 0]
    out: List[str] = []
    while ready:
        mid = ready.pop(0)
        out.append(mid)
        for dependent in dependents[mid]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    return out


def _cycle_members(residual: List[str],
                   deps: Dict[str, List[str]]) -> set:
    """The residual's members that are actually IN a cycle: an SCC of size > 1,
    or a self-loop. Tarjan, iterative so a deep graph cannot blow the stack."""
    inside = set(residual)
    index_of: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Dict[str, bool] = {}
    stack: List[str] = []
    counter = 0
    found: set = set()

    for root in residual:
        if root in index_of:
            continue
        work: List[Tuple[str, int]] = [(root, 0)]
        while work:
            node, next_child = work[-1]
            if next_child == 0:
                index_of[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack[node] = True
            children = [d for d in deps.get(node, []) if d in inside]
            if next_child < len(children):
                work[-1] = (node, next_child + 1)
                child = children[next_child]
                if child not in index_of:
                    work.append((child, 0))
                elif on_stack.get(child):
                    low[node] = min(low[node], index_of[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack[member] = False
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in deps.get(node, []):
                    found.update(component)
    return found


def _row(record: Dict[str, Any], deps: Dict[str, List[str]],
         missing: List[str], error: Optional[str]) -> Dict[str, Any]:
    manifest = record["manifest"]
    return {
        "id": record["id"],
        "title": manifest.get("title") or record["id"],
        "description": manifest.get("description", ""),
        "version": manifest.get("version", ""),
        # Always False for an installed mod, whatever the manifest declared.
        "default_enabled": False,
        "requires": list(manifest.get("requires", [])),
        "source": "installed",
        "gen": record["gen"],
        "scripts": list(manifest.get("scripts", [])),
        "styles": list(manifest.get("styles", [])),
        "integrity": {name: meta["integrity"]
                      for name, meta in record["files"].items()
                      if content_type(name) is not None},
        "error": error,
        "missing_requires": list(missing),
    }
