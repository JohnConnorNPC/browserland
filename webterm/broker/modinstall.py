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
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

# ---- names on disk ---------------------------------------------------------

#: Ownership marker. The destructive sweeps REFUSE to run in a directory that
#: does not carry it, so a ``mods_dir`` mis-set to an existing user directory
#: cannot have unrelated trees deleted out from under it.
MARKER_NAME = ".browserland-mods"
#: The atomic pointer naming the live generation: ``{"gen": "<sha256 hex>"}``.
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
    "no_mods_dir": 500,
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

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_CSS_IMPORT_RE = re.compile(r"@import\b", re.I)
_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_CSS_ABSOLUTE_RE = re.compile(r"\A\s*(?:[a-z][a-z0-9+.\-]*:)?//", re.I)
_CSS_HTTP_RE = re.compile(r"\A\s*https?:", re.I)


def _reject_css_external_references(name: str, text: str) -> None:
    """Refuse ``@import`` and any absolute ``url(http…)`` / ``url(//…)``.

    The app's CSP sets ONLY ``script-src`` and ``frame-ancestors`` -- there is
    no ``default-src`` and no ``style-src`` -- so a stylesheet's ``@import``,
    ``url()`` and ``@font-face`` are entirely unrestricted, and would be a
    SILENT egress channel out of a broker whose only outbound HTTP is the
    deliberately-closed ``/status/fetch``. This is defence in depth, not a
    boundary (the mod's own JS can ``fetch()`` anything, and #163 settled that
    ``ctx`` is not a boundary) -- but CSS egress is the silent one, and this
    preserves the "no third-party origin loads in this page" property #143/#146
    bought. ``data:`` and relative URLs pass.

    Comments are stripped first, exactly as a CSS parser would, so a commented
    -out ``@import`` is not a false refusal."""
    stripped = _CSS_COMMENT_RE.sub(" ", text)
    if _CSS_IMPORT_RE.search(stripped):
        raise ValidationError("css_external_reference",
                              f"{name}: @import may not be used")
    for match in _CSS_URL_RE.finditer(stripped):
        value = match.group(2)
        if _CSS_ABSOLUTE_RE.match(value) or _CSS_HTTP_RE.match(value):
            raise ValidationError(
                "css_external_reference",
                f"{name}: url({value[:64]}…) names an external origin")


# ---- per-file byte rules ---------------------------------------------------

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
    if text.count("\n") > cap:
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

    return {"id": mod_id,
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
                gen: str, installed_at: int) -> Dict[str, Any]:
    """One ``index["mods"]`` entry. ``help_md`` is captured HERE, at scan or
    install time, so Help is built from the very bytes being served instead of
    from a second traversal that could disagree."""
    help_rec = records.get(HELP_NAME)
    return {
        "id": manifest["id"],
        "gen": gen,
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


def read_current(mod_path: Path) -> Optional[str]:
    """The generation named by ``<mod>/CURRENT``, or ``None``.

    The pointer IS the commit, so this is deliberately strict: anything that is
    not a JSON object carrying a well-formed sha256 hex ``gen`` means "this mod
    has no live generation", which is the same answer a crash mid-install
    leaves behind."""
    try:
        with open(mod_path / CURRENT_NAME, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    gen = data.get("gen")
    return gen if isinstance(gen, str) and GEN_RE.fullmatch(gen) else None


def _read_generation(gen_path: Path, root_real: str
                     ) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], int]:
    """Read + validate one ``<id>/<gen>/`` directory. Raises ``ValidationError``.

    Files whose names fail the grammar, or whose suffix we do not ship, are
    IGNORED with a log rather than refusing the mod: a stray ``.DS_Store`` or a
    leftover editor backup is inert junk, and refusing a whole mod over it is
    hostile. A file the manifest actually DECLARES is a different matter -- it
    will not be in the map, and the declaration check refuses the mod."""
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
        try:
            raw = (gen_path / entry.name).read_bytes()
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
    try:
        with open(gen_path / MANIFEST_NAME, "r", encoding="utf-8") as fh:
            manifest_raw = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError,
            ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("bad_json",
                              f"{gen_path}/{MANIFEST_NAME}: {exc}") from None
    manifest, records = validate_package(manifest_raw, files)
    return manifest, records, _read_installed_at(gen_path)


def _read_installed_at(gen_path: Path) -> int:
    """``.gen.json``'s ``installed_at``, else the directory mtime.

    The sidecar is why the stamp survives a restore, a copy or a rescan -- a
    filesystem mtime does not. The mtime is only the fallback for a
    hand-populated generation, where there is no better answer and the stamp is
    honestly unstable."""
    try:
        with open(gen_path / GEN_META_NAME, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        stamp = meta.get("installed_at") if isinstance(meta, dict) else None
        if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0:
            return stamp
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        pass
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
    gen = read_current(mod_path)
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
    computed = compute_gen(manifest, records)
    if computed != gen:
        # Not fatal: the URL is whatever CURRENT names, and hand-population is
        # explicitly not a trust boundary. But it means these bytes were not
        # produced by an install, so say so once, loudly.
        LOGGER.warning("mods_dir: %r generation %s does not content-address to "
                       "its own bytes (computed %s) -- hand-populated?",
                       name, gen[:12], computed[:12])
    index["mods"][name] = make_record(manifest, records, gen, installed_at)
    index["assets"].update(assets_for(name, gen, records))
    _load_retained_generations(mod_path, name, gen, root_real, index)
    return None


def _load_retained_generations(mod_path: Path, mod_id: str, live_gen: str,
                               root_real: str, index: Dict[str, Any]) -> None:
    """Also serve the retained PREVIOUS generation's assets.

    A page that started booting against generation A must not be handed a file
    from generation B, and a broker restart in that window is the one case the
    install path's in-memory carry-over cannot cover. Assets only -- the
    catalog, Help and the policy all describe CURRENT and nothing else. A
    predecessor that no longer validates is simply dropped: it is not the live
    generation, so there is nothing to be loud about."""
    try:
        entries = sorted((e for e in os.scandir(mod_path)
                          if e.name != live_gen
                          and GEN_RE.fullmatch(e.name)
                          and e.is_dir(follow_symlinks=False)),
                         key=lambda e: e.stat().st_mtime, reverse=True)
    except OSError:
        return
    for entry in entries[:RETAINED_GENERATIONS - 1]:
        try:
            _, records, _ = _read_generation(mod_path / entry.name, root_real)
        except (ValidationError, OSError):
            continue
        index["assets"].update(assets_for(mod_id, entry.name, records))


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
