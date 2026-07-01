"""Sanic broker app: windowed desktop page, browser relay WS, producer WS,
/sessions, /profiles, profiles-only /launch.

Default bind is 127.0.0.1:4445 (4444 was an earlier broker's port).

Auth policy lives in auth.py. WS auth is checked IN-HANDLER, post-upgrade,
closing with code 4401: rejecting the upgrade from HTTP middleware surfaces
in the browser as an opaque close code 1006, indistinguishable from a
network failure (a lesson carried over from an earlier broker).

CORS posture: the UI's multi-host mode has the BROWSER fetch /sessions and
dial /ws directly on every configured broker, so the JSON API needs CORS.
ACAO is ``*`` emitted UNCONDITIONALLY on EVERY response (including 401/404/405
and on a tokenless broker) — auth is token-in-query/header, never cookies, so
``*`` introduces no ambient-credential risk and needs no Vary/origin-echo. It
was previously gated on a token being configured, but that left a tokenless
broker reachable over the LAN/Tailscale unable to answer the UI's cross-origin
/sessions fetch (the reported bug) even though any non-browser client could
already read it; CORS only ever governs *browser* reads, and the real gate is
network reachability plus the token on every mutation/data endpoint (/launch,
/file/*, /state are token-or-loopback gated). With a token configured a
cross-origin page cannot drive them (it carries no token). With NO token the
gate is loopback-only: a same-machine cross-origin page CAN reach them over
loopback and, because ACAO is ``*``, read the response — the accepted
single-user-loopback exposure (same as /launch). NOTE since #35 that exposure
is host-wide for /file/* (full host read/write, the same trust /launch already
grants by spawning shells), so a tokenless broker must not run while the same
browser visits untrusted sites. The header must ride on error responses too or a
cross-origin login probe surfaces as a fetch TypeError ("wrong password"
indistinguishable from "host down"). Preflights are explicit OPTIONS routes
(route resolution happens before request middleware, so middleware can't answer
them) and unauthenticated by design (they carry no credentials). AUTO_EXTEND is
pinned off: sanic-ext, when merely installed, silently injects its own CORS
middleware plus an unauthenticated /docs + /openapi.json.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from sanic import Request, Sanic, Websocket
from sanic.exceptions import NotFound
from sanic.response import empty, html, json as sanic_json

from .. import build_version, protocol
from . import auth, relay
from .launcher import LaunchError, Launcher, default_profiles
from .registry import BrokerRegistry, run_producer_session
# NB: .ui (INDEX_HTML) and .help_corpus (HELP_CORPUS) are imported lazily inside
# create_app, gated on serve_ui — headless brokers (#87) must never assemble the
# desktop page or parse the wiki. These are the only production importers, so the
# deferral is what actually skips the work.

LOGGER = logging.getLogger(__name__)

CONFIG_ENV = "WEB_TERMINAL_CONFIG"
DEFAULT_PORT = 4445

# Editor file-API: a single read/write is capped at this many bytes (the cap
# is enforced on the ENCODED payload, not the character count — correct for
# UTF-16's ~2x size).
MAX_FILE_BYTES = 5 * 2**20  # 5 MiB

# Chunked transfer (#108): the cross-host copy/move byte path and in-app
# download stream a file through /file/read_chunk + the /file/upload_* session
# endpoints, so the 5 MiB whole-file cap above no longer bounds them. Two caps
# keep per-request memory and per-session disk bounded:
#   MAX_CHUNK_BYTES     — the largest single ranged read, and the largest DECODED
#                         size of one upload chunk. One chunk (~5.3 MiB base64)
#                         is the most a broker or the browser holds in flight.
#   MAX_TRANSFER_BYTES  — cumulative decoded bytes one upload session may accept
#                         before it is dropped (backpressure vs disk exhaustion;
#                         cf. MAX_ARCHIVE_BYTES below).
MAX_CHUNK_BYTES = 4 * 2**20        # 4 MiB per ranged read / upload chunk
MAX_TRANSFER_BYTES = 2 * 2**30     # 2 GiB cumulative per upload session
# In-flight upload sessions (#108) live in-memory on app.ctx (the broker runs
# single_process, so one dict is authoritative — see __main__). Bound their
# count so idle/abandoned begins can't exhaust the table, and expire stale ones
# (a browser that closed mid-transfer never sends commit/abort) so their temp
# files don't linger.
MAX_UPLOAD_SESSIONS = 32
UPLOAD_SESSION_TTL = 3600.0        # seconds since begin before a sweep drops it


# Non-UTF-8 editor support (#97). The broker ships only sanic+websockets, so
# detection is STDLIB-only (no chardet) and BOM-based for the multibyte
# encodings — Windows Notepad always writes a BOM, and guessing BOM-less
# UTF-16 is exactly what turns binary into garbage text. Every multibyte label
# is reached ONLY via its BOM, so the label alone implies BOM presence and the
# save round-trip is byte-faithful with no separate `bom` flag.
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "utf-16-le", "utf-16-be",
                   "cp1252", "latin-1")


class _NotText(Exception):
    """raw bytes aren't decodable as a supported text encoding (UTF-32, or a
    binary blob). The /file/read caller maps it to the back-compat ``not_utf8``
    error code (which now means 'not supported text / looks binary')."""


def _looks_binary(raw: bytes) -> bool:
    """Heuristic 'binary, not text' check in a single pass over ``raw``.

    Primary signal: any NUL byte → binary. Compressed/encrypted/executable
    payloads almost always carry one, and no supported text encoding emits a
    lone NUL for a BOM-less file — so this both keeps the existing binary-blob
    test green and, critically, stops BOM-less ``A\\x00B\\x00`` UTF-16 from
    being mistaken for text. Secondary: a high ratio of non-text control bytes
    (< 0x20, excluding the usual whitespace + ESC) also marks it binary. The
    NUL guard is the only protection before the TOTAL latin-1 fallback."""
    if not raw:
        return False
    allowed = (0x09, 0x0a, 0x0c, 0x0d, 0x1b)   # \t \n \f \r ESC
    ctrl = 0
    for b in raw:
        if b == 0x00:
            return True                        # NUL → binary, decisive
        if b < 0x20 and b not in allowed:
            ctrl += 1
    return (ctrl / len(raw)) > 0.30


def _decode_file_text(raw: bytes):
    """Decode file bytes to ``(text, encoding_label)``; raises ``_NotText`` for
    UTF-32 / binary / a corrupt BOM. Detection order (#97): UTF-32 BOM (rejected
    first so ``ff fe 00 00`` can't be misread as UTF-16LE), UTF-8 BOM, UTF-16
    LE/BE BOM, then the binary guard, then BOM-less strict UTF-8, then cp1252,
    falling back to total latin-1 for cp1252's five undefined bytes."""
    if raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        raise _NotText("utf-32 unsupported")
    # A declared BOM that doesn't actually decode (truncated/odd-length UTF-16,
    # invalid UTF-8 after the BOM) is corrupt, not text — map the
    # UnicodeDecodeError to _NotText so the route returns not_utf8 rather than
    # 500ing on an unhandled exception.
    try:
        if raw[:3] == codecs.BOM_UTF8:             # ef bb bf
            return raw[3:].decode("utf-8"), "utf-8-sig"
        if raw[:2] == codecs.BOM_UTF16_LE:         # ff fe
            return raw[2:].decode("utf-16-le"), "utf-16-le"
        if raw[:2] == codecs.BOM_UTF16_BE:         # fe ff
            return raw[2:].decode("utf-16-be"), "utf-16-be"
    except UnicodeDecodeError:
        raise _NotText("declared BOM but undecodable") from None
    # No BOM. The binary guard runs BEFORE the UTF-8 decode so a BOM-less
    # UTF-16 / embedded-NUL file rejects cleanly as not_utf8 instead of
    # decoding into NUL-riddled garbage text (a NUL is valid UTF-8 but never
    # appears in real text). It also gates the total latin-1 fallback below.
    if _looks_binary(raw):
        raise _NotText("looks binary")
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        # cp1252 leaves 81 8d 8f 90 9d undefined; latin-1 is total (no raise).
        return raw.decode("latin-1"), "latin-1"


def _encode_file_text(content: str, encoding_label: str) -> bytes:
    """Encode editor text back to bytes for ``encoding_label`` (re-adding the
    BOM for the BOM-implying labels) — the inverse of ``_decode_file_text``, so
    an unedited file round-trips byte-identically. Caller MUST pre-validate the
    label against ``_TEXT_ENCODINGS`` (an unknown one is a programming error,
    KeyError). May raise ``UnicodeEncodeError`` when edited text gains a char a
    legacy encoding (cp1252/latin-1) can't store — the caller maps that to
    ``encode_failed`` and prompts to save as UTF-8 (never a silent convert)."""
    if encoding_label == "utf-8":
        return content.encode("utf-8")
    if encoding_label == "utf-8-sig":
        return content.encode("utf-8-sig")     # re-adds the UTF-8 BOM
    if encoding_label == "utf-16-le":
        return codecs.BOM_UTF16_LE + content.encode("utf-16-le")
    if encoding_label == "utf-16-be":
        return codecs.BOM_UTF16_BE + content.encode("utf-16-be")
    if encoding_label == "cp1252":
        return content.encode("cp1252")
    if encoding_label == "latin-1":
        return content.encode("latin-1")
    raise KeyError(encoding_label)

# /file/zip + /file/unzip caps (#72). Bound the work a single archive op will do
# so a hostile (or accidental) huge source tree or zip-bomb can't exhaust disk
# or memory: cumulative UNCOMPRESSED size and member/entry count are both
# pre-scanned and rejected BEFORE any write or extract. Tunable.
MAX_ARCHIVE_BYTES = 1 * 2**30      # 1 GiB cumulative uncompressed
MAX_ARCHIVE_ENTRIES = 50000        # member/entry count

# /state: the shared per-broker UI settings+layout blob is small (a layout
# tree + a settings object); cap the serialized JSON so a hostile PUT can't
# balloon the on-disk store.
MAX_STATE_BYTES = 2 * 2**20  # 2 MiB

# /session/* management RPCs: how long the broker waits for the producer's
# reply before giving up with 504 (the agent does psutil/git work off its
# event loop, so this is generous).
RPC_TIMEOUT = 10.0

# Valid per-window / default MCP access modes.
MCP_MODES = ("off", "read", "readwrite")

# /mcp/input: cap one input frame's UTF-8 payload. Terminal input (keystrokes,
# a pasted command) is tiny; this just stops a readwrite MCP token from
# enqueueing an unbounded write to the PTY. Generous vs any real input.
MAX_MCP_INPUT_BYTES = 256 * 1024

# /mcp/read wait-for-change (#26): cap how long the agent may hold a read while
# waiting for the screen to change. This also bounds how long one waiting read
# occupies a per-session RPC slot (RPC_MAX_INFLIGHT); the agent clamps to the
# same ceiling.
MAX_MCP_WAIT_MS = 15000

# Launch-profile editor (#70, POST /profiles/config). A same-machine browser-
# realm page can drive this (the accepted /file/* posture), and each profile is
# a persistent shell recipe /launch will spawn by name — so hard-cap the write:
# generous for any real shell menu, small enough a hostile page can't balloon
# the sidecar or smuggle control chars/oversized names into the UI/logs.
MAX_PROFILES_BYTES = 256 * 1024
MAX_PROFILES = 200
MAX_PROFILE_COMMAND = 64          # argv tokens per profile
MAX_PROFILE_TOKEN = 4096          # chars per argv token / cwd
MAX_PROFILE_TITLE = 256
# Names key the sidecar, title windows, and show in the UI: a boring charset
# (no control chars, quotes, slashes, HTML, or bidi) with a length cap. fullmatch
# rejects a trailing newline that ``$`` would allow.
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9 ._+-]{1,64}")


def _norm_mcp_mode(value: Any, default: str = "off") -> str:
    """Coerce an arbitrary value to a valid MCP mode, else ``default``."""
    v = str(value or "").strip().lower()
    return v if v in MCP_MODES else default


def _empty_state() -> Dict[str, Any]:
    return {"rev": 0, "settings": {}, "layout": {}}


def _load_mcp_cfg(path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """Build the live MCP runtime config: ``{token, default_mode, allow_launch,
    enabled}``.

    Seeded from config/env defaults, then overlaid by the persisted sidecar
    (``webterm_mcp.json``) — the sidecar is what the Control Panel writes, so it
    is the durable source of truth across restarts. One exception, mirroring
    resolve_token's "env wins so a unit file can override": if the env token
    ``WEB_TERMINAL_MCP_TOKEN`` is set it pins the token even over the sidecar.
    Every field self-heals, so a hand-edited/truncated sidecar can never break
    startup. The per-window overrides themselves are in-memory only (they live
    on WindowEntry and reset on restart); only these broker-wide knobs persist."""
    env_token = os.environ.get(auth.MCP_TOKEN_ENV)
    cfg = {
        "token": auth.resolve_mcp_token(config),
        "default_mode": _norm_mcp_mode(config.get("mcp_default_mode"), "off"),
        "allow_launch": bool(config.get("mcp_allow_launch", False)),
        "enabled": bool(config.get("mcp_enabled", False)),
    }
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        if "token" in data and not env_token:
            tok = data.get("token")
            cfg["token"] = tok if isinstance(tok, str) and tok else None
        if "default_mode" in data:
            cfg["default_mode"] = _norm_mcp_mode(
                data.get("default_mode"), cfg["default_mode"])
        if "allow_launch" in data:
            cfg["allow_launch"] = bool(data.get("allow_launch"))
        if "enabled" in data:
            cfg["enabled"] = bool(data.get("enabled"))
    return cfg


def _valid_profile_entry(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce one raw profile value into a clean ``{command, title?, cwd?}``
    entry, or ``None`` if it can't be salvaged. ``command`` (the argv the
    launcher runs — RCE-by-design, never client-supplied) must be a non-empty
    list of non-empty strings; a single bad token drops the WHOLE profile so a
    half-mangled command can never run. ``title``/``cwd`` are optional and
    self-heal to sane types. Used by the sidecar loader to self-heal; POST
    /profiles/config validates STRICTLY (rejects rather than coerces) before
    anything reaches disk."""
    if not isinstance(value, dict):
        return None
    command = value.get("command")
    if not isinstance(command, list) or not command:
        return None
    for part in command:
        if not isinstance(part, str) or not part:
            return None
    entry: Dict[str, Any] = {"command": [str(p) for p in command]}
    title = value.get("title")
    entry["title"] = title[:256] if isinstance(title, str) and title else None
    cwd = value.get("cwd")
    entry["cwd"] = cwd if isinstance(cwd, str) and cwd else None
    return entry


def _heal_profiles(raw: Any) -> Dict[str, Any]:
    """Only the salvageable ``{name: {command,...}}`` entries from a raw profiles
    mapping, dropping anything malformed (bad name or unsalvageable command), so
    a hand-edited/truncated sidecar or config can never break startup or
    /launch. Names must be non-empty short strings without control characters
    (they title windows and key the sidecar/UI)."""
    out: Dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        if not isinstance(name, str):
            continue
        n = name.strip()
        if not n or len(n) > 64 or any(ord(c) < 0x20 for c in n):
            continue
        entry = _valid_profile_entry(value)
        if entry is not None:
            out[n] = entry
    return out


def _resolve_default_profile(default_profile: Any, profiles: Dict[str, Any],
                             fallback: str) -> str:
    """Coerce ``default_profile`` to a real member of ``profiles`` so a launch
    with no explicit profile always resolves: prefer the requested value, then
    the seed fallback, then any key. Empty only when there are no profiles."""
    if isinstance(default_profile, str) and default_profile in profiles:
        return default_profile
    if fallback in profiles:
        return fallback
    return next(iter(profiles), "")


def _load_profiles_cfg(path: Path, config: Dict[str, Any]) -> Dict[str, Any]:
    """Build the live launch-profile set: ``{profiles, default_profile,
    source}``.

    Seed = the config's ``agent`` block (profiles/default_profile) if usable,
    else the built-in per-OS defaults. Then, IF the sidecar
    (``webterm_profiles.json``) exists and holds at least one valid profile, the
    SIDECAR OWNS the whole set (sidecar-owns-once-written) — broker_config.json's
    ``agent.profiles`` is only the seed, so add/edit/delete/rename all persist
    cleanly across restarts (mirrors _load_mcp_cfg's sidecar-is-truth posture).
    Every field self-heals (malformed entries dropped, default coerced to a real
    member), so a truncated/hand-edited sidecar can never break startup or brick
    /launch; a sidecar with NO salvageable profile falls back to the seed rather
    than leaving zero shells to launch."""
    defaults = default_profiles()
    agent = config.get("agent")
    agent = agent if isinstance(agent, dict) else {}
    seed_profiles = _heal_profiles(agent.get("profiles")) \
        or _heal_profiles(defaults["profiles"])
    seed_default = agent.get("default_profile")
    if not (isinstance(seed_default, str) and seed_default):
        seed_default = defaults["default_profile"]

    profiles = seed_profiles
    default_profile = seed_default
    source = "config"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        healed = _heal_profiles(data.get("profiles"))
        if healed:                       # sidecar owns once it holds >=1 profile
            profiles = healed
            default_profile = data.get("default_profile", seed_default)
            source = "sidecar"

    default_profile = _resolve_default_profile(
        default_profile, profiles, seed_default)
    return {"profiles": profiles, "default_profile": default_profile,
            "source": source}


def _validate_profile_command(command: Any) -> Optional[str]:
    """Strictly validate one profile's ``command`` argv: a non-empty list of
    non-empty, control-char-free, length-capped strings. Returns an error slug
    or ``None``. Control chars (incl. NUL/newline/tab) are rejected — no legit
    shell argv needs one, and NUL would make the spawn fail later anyway."""
    if not isinstance(command, list) or not command:
        return "bad_command"
    if len(command) > MAX_PROFILE_COMMAND:
        return "command_too_long"
    for part in command:
        if not isinstance(part, str) or not part:
            return "bad_command"
        if len(part) > MAX_PROFILE_TOKEN:
            return "command_token_too_long"
        if any(ord(c) < 0x20 for c in part):
            return "bad_command"
    return None


def _validate_profiles_post(body: Dict[str, Any]):
    """Validate a POST /profiles/config body into a clean ``{profiles,
    default_profile}``, or return ``(None, error_slug)``. REPLACE semantics: the
    body defines the WHOLE set, so it REJECTS (never coerces) — a bad field
    changes nothing. Empty ``profiles`` is rejected (``no_profiles``) so an edit
    can't leave zero shells and brick /launch; ``default_profile`` is resolved to
    a real member so a no-explicit-profile launch always resolves."""
    profiles_in = body.get("profiles")
    if not isinstance(profiles_in, dict):
        return None, "bad_profiles"
    if not profiles_in:
        return None, "no_profiles"
    if len(profiles_in) > MAX_PROFILES:
        return None, "too_many_profiles"
    clean: Dict[str, Any] = {}
    for name, value in profiles_in.items():
        if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
            return None, "bad_name"
        if not isinstance(value, dict):
            return None, "bad_profile"
        cmd_err = _validate_profile_command(value.get("command"))
        if cmd_err:
            return None, cmd_err
        entry: Dict[str, Any] = {"command": list(value.get("command"))}
        title = value.get("title")
        if title is not None and not isinstance(title, str):
            return None, "bad_title"
        if isinstance(title, str) and len(title) > MAX_PROFILE_TITLE:
            return None, "title_too_long"
        entry["title"] = title or None
        cwd = value.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                return None, "bad_cwd"
            if len(cwd) > MAX_PROFILE_TOKEN:
                return None, "cwd_too_long"
            if any(ord(c) < 0x20 for c in cwd):
                return None, "bad_cwd"
        entry["cwd"] = cwd or None
        clean[name] = entry
    default_profile = body.get("default_profile", "")
    if default_profile is None:
        default_profile = ""
    if not isinstance(default_profile, str):
        return None, "bad_default"
    if default_profile and default_profile not in clean:
        return None, "default_not_member"
    default_profile = _resolve_default_profile(
        default_profile, clean, next(iter(clean)))
    return {"profiles": clean, "default_profile": default_profile}, None


# ---- launch-profile detection (#70, GET /profiles/detect) ----------------
# Best-effort scan for launchable shells to SEED the Control Panel editor. The
# user confirms every suggestion before it is saved, so this is read-only and
# NEVER raises: a missing tool / timeout / weird output yields fewer (or zero)
# suggestions. Detection subprocesses run off the event loop (executor).

# POSIX shells worth suggesting, by bare name — an allow-list so /etc/shells
# entries like /usr/sbin/nologin or /bin/false are never proposed.
_POSIX_SHELLS = ("bash", "zsh", "fish", "sh")
_WSL_NAME_MAX = 64


def _wsl_exe() -> Optional[str]:
    """Prefer the canonical System32 wsl.exe over a bare PATH lookup: a PATH hit
    could resolve an attacker-planted wsl.exe earlier in PATH. Falls back to
    ``shutil.which`` only if the System32 copy is absent."""
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    cand = os.path.join(root, "System32", "wsl.exe")
    if os.path.isfile(cand):
        return cand
    return shutil.which("wsl.exe")


def _detect_windows_shells() -> List[Dict[str, Any]]:
    exe = _wsl_exe()
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "-l", "-q"], capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    # `wsl -l -q` prints registered distro names as UTF-16-LE, often with a BOM,
    # one per line. Decode leniently, strip BOM/NULs, and keep only plausible
    # single-token names: this drops the localized "has no installed
    # distributions" sentence (it carries spaces) and any control junk. Names are
    # UNTRUSTED strings, but they only ever ride an argv element (never a shell
    # string), so there is no injection — the caps just keep the UI/logs clean.
    text = proc.stdout.decode("utf-16-le", errors="ignore").replace("\x00", "")
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in text.splitlines():
        name = raw.strip().lstrip("﻿").strip()
        if not name or name in seen:
            continue
        if len(name) > _WSL_NAME_MAX or any(ord(c) < 0x20 for c in name):
            continue
        if any(c.isspace() for c in name):     # drops the no-distro sentence
            continue
        seen.add(name)
        # The recipe uses the bare "wsl.exe" (a name PATH-resolved at launch by
        # the agent), NOT the machine-specific System32 path used above.
        out.append({
            "name": name, "title": f"{name} (WSL)",
            "command": ["wsl.exe", "-d", name, "--cd", "~", "--", "bash", "-l"],
            "exists": True,
        })
    return out


def _detect_posix_shells() -> List[Dict[str, Any]]:
    # Union of the allow-listed shells found on PATH and those listed in
    # /etc/shells (basename must be allow-listed AND the path must exist), so a
    # commented / bogus / nologin entry is never suggested. Deduped by shell name.
    found = {}
    for name in _POSIX_SHELLS:
        path = shutil.which(name)
        if path:
            found[name] = path
    try:
        with open("/etc/shells", "r", encoding="utf-8", errors="ignore") as fh:
            lines = fh.read().splitlines()
    except (OSError, ValueError):
        lines = []
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        base = os.path.basename(entry)
        if base in _POSIX_SHELLS and base not in found and os.path.isfile(entry):
            found[base] = entry
    out: List[Dict[str, Any]] = []
    for name in _POSIX_SHELLS:                 # stable, allow-list order
        if name in found:
            out.append({"name": name, "title": name,
                        "command": [name, "-l"], "exists": True})
    return out


def _detect_profile_suggestions() -> List[Dict[str, Any]]:
    """Per-OS launchable-shell suggestions ({name,title,command,exists}). Runs in
    an executor (blocking subprocess/FS). Never raises — returns [] on any
    trouble."""
    try:
        return _detect_windows_shells() if os.name == "nt" \
            else _detect_posix_shells()
    except Exception:      # defensive: detection must never break the endpoint
        LOGGER.warning("profile detection failed", exc_info=True)
        return []


def _load_state(path: Path) -> Dict[str, Any]:
    """Read the persisted {rev, settings, layout} blob, self-healing every
    field so a hand-edited or truncated file can never break startup. ``rev``
    is persisted (not in-memory only) so a broker restart never resets it and
    re-accepts a stale client's baseRev (the loser-false-accept hazard)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    rev = data.get("rev")
    settings = data.get("settings")
    layout = data.get("layout")
    return {
        "rev": rev if isinstance(rev, int) and rev >= 0 else 0,
        "settings": settings if isinstance(settings, dict) else {},
        "layout": layout if isinstance(layout, dict) else {},
    }


def _write_state_atomic(path: Path, state: Dict[str, Any]) -> None:
    """Atomic replace (same pattern as /file/write): temp in the same dir, swap
    via os.replace so a reader never sees a half-written file."""
    parent = path.parent
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".webterm-state-",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, str(path))
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _load_or_create_broker_id(path: Path) -> str:
    """This broker's stable identity (a uuid4 hex), persisted in a standalone
    file (``webterm_identity.json``) beside the state store. Minted + written on
    first run, then immutable across restarts.

    Deliberately kept OUT of ``webterm_state.json`` (the {rev,settings,layout}
    blob that syncs to clients and bumps every save) and the MCP sidecar, so the
    id is never tied to the rev cycle and never round-trips through /state. Used
    ONLY for duplicate-broker warnings and to gate the terminate fallback (#64);
    it is non-secret and never an authorization input, so a hand-edited or
    truncated file simply self-heals by re-minting (no startup break)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            bid = data.get("broker_id")
            if isinstance(bid, str) and bid:
                return bid
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        pass
    bid = uuid.uuid4().hex
    try:
        _write_state_atomic(path, {"broker_id": bid})
    except OSError:
        # Read-only dir / disk full: serve a process-local id this run rather
        # than crash. It will differ on the next restart, at worst re-showing a
        # duplicate warning — never an auth or correctness failure.
        pass
    return bid


def _resolve_host_path(rel: str, default_dir: Path,
                       follow_leaf: bool = True) -> Path:
    """Resolve a client-supplied file path **host-wide** — anywhere on this box,
    with NO ``editor_root`` confinement (#35).

    The file API shares the EXACT auth gate as ``/launch`` (token when
    configured, else loopback-only), and an authenticated client already has
    full filesystem access through its terminal shells — so sandboxing the file
    tools adds friction without adding security. Browsing is therefore host-wide,
    gated only by that auth + per-host routing (the same single-user threat model
    the AGENTS.md carve-out, #16, already accepted for two filenames; this just
    generalises it to every file).

    Resolution rules (cross-platform; deliberately strict about the half-absolute
    Windows spellings pathlib would otherwise join surprisingly — codex review):
      - empty ``rel`` -> ``default_dir`` (the initial dir, e.g. a terminal cwd).
      - a fully-absolute path (POSIX ``/x``; Windows ``C:\\x`` or
        ``\\\\srv\\share``) is taken as-is.
      - a *drive-relative* (``C:foo``) or *rooted-relative* (``\\foo``) path —
        ``drive`` or ``root`` set but not BOTH, i.e. not ``is_absolute()`` — is
        rejected, because joining it onto ``default_dir`` would jump to a drive
        root instead of staying under it.
      - any other relative path joins onto ``default_dir``.
    ``resolve()`` then collapses ``..`` and follows symlinks (escaping the start
    dir is the POINT here, not a bypass to defend against). A colon in any
    non-anchor component (``file:ads``, ``dir:x\\f``) is an NTFS
    alternate-data-stream spelling and is rejected. Resolver failures (symlink
    loop, bad drive) raise ``ValueError`` -> the caller maps to ``bad_path``.

    ``follow_leaf`` (#72, default ``True``) keeps the full ``resolve()`` — i.e.
    every existing caller is byte-identical. With ``follow_leaf=False`` the
    PARENT is resolved (symlinks higher in the path still collapse) and the raw
    leaf name is re-attached, so a symlink or junction AT the leaf is *preserved*
    for the caller to handle rather than dereferenced. This is load-bearing for
    the destructive ops: a naive ``rmtree``/``rename``/``move`` of a fully
    resolved symlink-to-dir would operate on the link's TARGET tree (host-wide
    data loss); link-safe resolution hands the caller the link itself. The ADS
    and half-absolute rejections apply identically in both modes."""
    raw = rel or ""
    base = Path(raw)
    # ADS guard (Windows only — ':' is a legal filename char on POSIX): drop the
    # drive/anchor (``C:`` / ``\\\\srv\\share``); any ':' left in the remainder is
    # an NTFS alternate-data-stream marker, never a path separator.
    if os.name == "nt" and ":" in raw[len(base.drive):]:
        raise ValueError("bad_path")
    if not raw:
        p = default_dir
    elif base.is_absolute():
        p = base
    elif base.drive or base.root:
        raise ValueError("bad_path")        # half-absolute: C:foo / \foo
    else:
        p = default_dir / base
    try:
        if follow_leaf:
            return p.resolve()
        # Link-safe leaf: resolve the parent, re-attach the raw leaf name. A path
        # that is its own anchor (no leaf name, e.g. ``C:\``) has nothing to
        # preserve, so fall back to a full resolve.
        name = p.name
        if not name:
            return p.resolve()
        return p.parent.resolve() / name
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("bad_path") from exc


def _is_reparse_point(path_str: str) -> bool:
    """True if the leaf at ``path_str`` is a symlink OR (Windows) a junction /
    other reparse point. ``os.path.islink`` alone misses junctions on Python
    < 3.12, so the reparse-point attribute bit is checked too — a destructive op
    must treat a junction-to-dir like a link (remove the entry, never recurse
    into its target). Best-effort: any stat failure returns False and the
    caller's normal classification handles it."""
    try:
        if os.path.islink(path_str):
            return True
    except OSError:
        return False
    if os.name == "nt":
        try:
            attrs = os.lstat(path_str).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    return False


def _set_windows_attributes(path_str: str, toggles: Dict[str, bool]) -> None:
    """#96: set/clear READONLY/HIDDEN/ARCHIVE via SetFileAttributesW. os.chmod on
    Windows only flips read-only, so the others need Win32. Read-modify-write so
    DIRECTORY/REPARSE/COMPRESSED and other settable bits survive. Raises OSError on
    failure to keep the {ok:false,error} contract.

    A FRESH WinDLL(..., use_last_error=True) (not the cached ctypes.windll handle,
    which has no use_last_error) with explicit arg/restypes so get_last_error()
    reflects THIS call and WinError carries the real Win32 code (C1)."""
    import ctypes                                   # Windows-only; not at module top
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    k32.GetFileAttributesW.restype = ctypes.c_uint32
    k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    k32.SetFileAttributesW.restype = ctypes.c_int
    cur = k32.GetFileAttributesW(path_str)
    if cur == 0xFFFFFFFF:                           # INVALID_FILE_ATTRIBUTES
        raise ctypes.WinError(ctypes.get_last_error())
    bits = {"readonly": stat.FILE_ATTRIBUTE_READONLY,
            "hidden":   stat.FILE_ATTRIBUTE_HIDDEN,
            "archive":  stat.FILE_ATTRIBUTE_ARCHIVE}
    new = cur
    for name, flag in bits.items():
        if name in toggles:
            new = (new | flag) if toggles[name] else (new & ~flag)
    if new == 0:
        new = stat.FILE_ATTRIBUTE_NORMAL            # 0 -> ERROR_INVALID_PARAMETER
    if k32.SetFileAttributesW(path_str, new) == 0:
        raise ctypes.WinError(ctypes.get_last_error())


def _remove_link(path_str: str) -> None:
    """Remove a symlink / junction ENTRY without touching its target. A
    directory-type link (dir symlink or junction) needs ``os.rmdir`` on Windows
    — ``os.unlink`` raises on it — while a file symlink (and a broken link) need
    ``os.unlink``. ``os.path.isdir`` follows the link to choose; a broken link
    (isdir False) takes the unlink path."""
    if os.path.isdir(path_str):
        os.rmdir(path_str)
    else:
        os.unlink(path_str)


def _force_remove(path_str: str) -> None:
    """Remove any leaf — a symlink/junction (entry only, never the target), a
    real file, or a real directory tree. Used by move-overwrite and recursive
    delete. The reparse-point check comes first so a link is never dereferenced
    into an rmtree of its target."""
    if _is_reparse_point(path_str):
        _remove_link(path_str)
    elif os.path.isdir(path_str):
        shutil.rmtree(path_str)
    else:
        os.unlink(path_str)


def _rename_or_move(src_str: str, dst_str: str) -> None:
    """Move ``src`` onto a NON-EXISTENT ``dst``: ``os.replace`` (atomic on one
    volume; moves a symlink/junction as the entry, not the target), falling back
    to ``shutil.move`` only on a cross-device error (EXDEV)."""
    try:
        os.replace(src_str, dst_str)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EXDEV:
            shutil.move(src_str, dst_str)
        else:
            raise


def _resolve_two(body: Dict[str, Any], default_dir: Path,
                 src_follow_leaf: bool = True,
                 dst_follow_leaf: bool = True):
    """Resolve the ``src`` and ``dst`` string fields of a copy/move body
    host-wide. Returns ``(src, dst)`` Paths, or raises ``ValueError`` (mapped to
    ``bad_path`` by the caller) on a missing / non-string field or a resolver
    failure. ``*_follow_leaf`` pick link-safe leaf resolution per side: move
    resolves both link-safe (so it relocates a link entry, not its target); copy
    follows (it is non-destructive to the source)."""
    src_rel = body.get("src")
    dst_rel = body.get("dst")
    if not isinstance(src_rel, str) or not src_rel:
        raise ValueError("bad_path")
    if not isinstance(dst_rel, str) or not dst_rel:
        raise ValueError("bad_path")
    src = _resolve_host_path(src_rel, default_dir, follow_leaf=src_follow_leaf)
    dst = _resolve_host_path(dst_rel, default_dir, follow_leaf=dst_follow_leaf)
    return src, dst


def _is_within(child: Path, ancestor: Path) -> bool:
    """True if ``child`` is ``ancestor`` or lives under it (case-insensitive on
    Windows via the pure-path compare). Refuses copying/moving a tree into
    itself — which would recurse infinitely and litter a partial copy."""
    try:
        return child.is_relative_to(ancestor)
    except (ValueError, TypeError):
        return False


def _classify_path(p: Path) -> str:
    """Classify a resolved path for the file API without letting a *denied* stat
    escape to a 500. Returns 'file' | 'dir' | 'other' | 'missing' | 'denied'.

    pathlib's ``exists()``/``is_file()``/``is_dir()`` already map the ignorable
    errnos (ENOENT/ENOTDIR/ELOOP, and their Windows equivalents) to ``False``,
    but a refused stat (EACCES / Windows ERROR_ACCESS_DENIED) raises — and with
    no global handler that surfaced as a 500 + traceback instead of the
    ``{"ok": false, "error": ...}`` contract the rest of these handlers keep
    (#46 review). Probe once here and report 'denied' so callers map it cleanly."""
    try:
        if p.is_file():
            return "file"
        if p.is_dir():
            return "dir"
        return "other" if p.exists() else "missing"
    except OSError:
        return "denied"


def _sweep_upload_sessions(uploads: Dict[str, Any], now: float) -> None:
    """Drop chunked-upload sessions (#108) older than ``UPLOAD_SESSION_TTL`` and
    best-effort unlink their temp files. Called lazily on each upload_begin so a
    transfer the browser abandoned (closed before commit/abort) can't leak a temp
    file or permanently hold a session slot. Keyed by ``created`` (not last-write)
    so a genuinely stuck/idle session is reclaimed even if it never appended."""
    stale = [uid for uid, s in uploads.items()
             if now - s.get("created", now) > UPLOAD_SESSION_TTL]
    for uid in stale:
        s = uploads.pop(uid, None)
        if s:
            try:
                os.unlink(s["tmp"])
            except OSError:
                pass


def _sweep_orphan_parts(parent: Path, now: float) -> None:
    """Best-effort removal of stale ``.webterm-up-*.part`` temp files directly
    under ``parent`` (#108). A crash/kill orphans a session's temp on disk without
    ever running the in-memory sweep; scanning the ONE dir an upload_begin is
    about to write catches those. Only files older than the TTL are removed, so an
    active young session's temp (recent mtime) is never touched. Any error is
    swallowed — host-wide, so a dir we can't scan simply isn't swept."""
    try:
        candidates = list(parent.glob(".webterm-up-*.part"))
    except OSError:
        return
    for child in candidates:
        try:
            if now - child.stat().st_mtime > UPLOAD_SESSION_TTL:
                child.unlink()
        except OSError:
            pass


def _json_object_body(request: "Request") -> Optional[Dict[str, Any]]:
    """Parsed JSON object body, or None on malformed / non-object JSON. An
    empty body is treated as ``{}`` (mirrors the /launch handler)."""
    if not request.body:
        return {}
    try:
        parsed = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """broker_config.json, path from $WEB_TERMINAL_CONFIG or alongside the
    package's repo root. Missing file -> defaults."""
    candidates = []
    if path:
        candidates.append(Path(path))
    elif os.environ.get(CONFIG_ENV):
        candidates.append(Path(os.environ[CONFIG_ENV]))
    else:
        candidates.append(Path(__file__).resolve().parents[2]
                          / "broker_config.json")
        candidates.append(Path.cwd() / "broker_config.json")
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                config = json.load(fh)
            LOGGER.info("loaded config from %s", candidate)
            return config if isinstance(config, dict) else {}
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"error: cannot read config {candidate}: {exc}")
    return {}


async def _index(request: Request):
    return html(request.app.ctx.index_html)


async def _help_corpus(request: Request):
    # The in-app Help guide's static cards, parsed from wiki/*.md (issue #60).
    # Public like "/" (help content is non-sensitive); built once at import in
    # help_corpus.py, so a wiki edit needs a broker restart to show up.
    return sanic_json(request.app.ctx.help_corpus)


async def _index_headless(request: Request):
    # Headless broker (serve_ui=False, #87): no desktop page is served. JSON so a
    # client hitting GET / can tell the UI is intentionally absent, not just missing.
    return sanic_json({"ui": False})


async def _handle_404(request: Request, exception):
    return html("<h1>404 - Page Not Found</h1>", status=404)


def create_app(config: Optional[Dict[str, Any]] = None,
               port: int = DEFAULT_PORT,
               name: str = "webterm-broker") -> Sanic:
    config = config or {}
    app = Sanic(name)
    # Browser pastes arrive as one input/paste frame and producer snapshots
    # scale with --ring-bytes; Sanic's default WEBSOCKET_MAX_SIZE (1 MiB)
    # kills either socket with a 1009 close and the bytes silently vanish
    # (Linux verification finding F2). 16 MiB bounds memory while clearing
    # any realistic paste or snapshot; the UI additionally chunks its sends.
    app.config.WEBSOCKET_MAX_SIZE = 16 * 2**20
    # sanic-ext auto-loads when merely installed (it is NOT one of our
    # dependencies), adding its own CORS middleware, auto-OPTIONS/HEAD, and
    # an unauthenticated /docs + /openapi.json. Pin it off so every install
    # behaves like a clean one; CORS is hand-rolled below.
    app.config.AUTO_EXTEND = False
    app.ctx.config = config
    app.ctx.auth_token = auth.resolve_token(config)
    # This broker's build id (#22): surfaced in /mcp/info and used as the
    # baseline to flag a producer whose reported version differs as stale.
    app.ctx.version = build_version()
    # Frontend mod-system master switch (#71). Mirrors the mcp_enabled posture
    # but defaults ON: the first-wave mods are first-party, in-repo and reviewed
    # (the clock ships as one), so an out-of-the-box install runs them. Surfaced
    # via /info so the loader can gate at runtime (fail-open / default-on).
    app.ctx.mods_enabled = bool(config.get("mods_enabled", True))
    # Headless mode (#87): when off, the broker serves the full JSON/WS API but
    # not the desktop page (GET /) or the in-app Help corpus, and skips
    # assembling both UI constants entirely. Defaults ON so existing deploys are
    # unchanged. The --headless CLI flag folds into config before we read it.
    app.ctx.serve_ui = bool(config.get("serve_ui", True))
    app.ctx.registry = BrokerRegistry()
    # The Launcher (profiles-only /launch source of truth) is constructed AFTER
    # the state path is resolved, so its sidecar (webterm_profiles.json, #70) can
    # sit beside the state store. See the "launch profiles" block below.
    # Editor file-API DEFAULT directory (NOT a sandbox, #35). The file tools
    # browse the whole host (same auth gate as /launch, which already grants
    # shell-level filesystem access — see _resolve_host_path); this is only the
    # dir an empty path resolves to, i.e. where Open/Save lands when no terminal
    # cwd was supplied. Default = the broker's CWD (the box it runs on — a
    # single-user loopback tool); override with "editor_root" in the config.
    # Resolved once into a stable, symlink-collapsed absolute path.
    app.ctx.editor_root = Path(
        config.get("editor_root") or os.getcwd()).resolve()
    LOGGER.info("editor file-API default dir: %s", app.ctx.editor_root)
    # In-flight chunked-upload sessions (#108), keyed by upload_id. Each value is
    # {tmp, dest, overwrite, received, created}. Populated by /file/upload_begin,
    # appended by /file/upload_chunk, drained by /file/upload_commit|abort, and
    # swept (lazily on begin + on shutdown) so an abandoned transfer's temp file
    # never lingers. Single_process => this one dict is the source of truth.
    app.ctx.uploads = {}
    # Shared per-broker UI state (settings + layout) for /state. Persisted as
    # JSON beside the broker config (override with "state_path"); rev lives in
    # the file so a restart preserves optimistic-concurrency ordering. The lock
    # serializes the read-rev / compare / write / bump sequence in PUT (the
    # file write awaits, so two PUTs could otherwise interleave on rev).
    app.ctx.state_path = Path(
        config.get("state_path") or (Path(os.getcwd()) / "webterm_state.json")
    ).resolve()
    app.ctx.state = _load_state(app.ctx.state_path)
    app.ctx.state_lock = asyncio.Lock()
    LOGGER.info("UI state store: %s (rev %s)",
                app.ctx.state_path, app.ctx.state["rev"])
    # Launch profiles (#70). The profiles-only allow-list the Control Panel edits
    # live. Persisted in a sidecar beside /state (override with
    # "profiles_state_path"), seeded from broker_config's agent.profiles. Once the
    # sidecar holds >=1 valid profile it OWNS the set (sidecar-owns-once-written,
    # like webterm_mcp.json); broker_config becomes the seed only. The lock
    # serializes the validate/write/live-swap in POST /profiles/config. The
    # Launcher stays the single source of truth for /profiles and every launch.
    app.ctx.profiles_path = Path(
        config.get("profiles_state_path")
        or (app.ctx.state_path.parent / "webterm_profiles.json")
    ).resolve()
    _pcfg = _load_profiles_cfg(app.ctx.profiles_path, config)
    app.ctx.profiles_source = _pcfg["source"]
    app.ctx.profiles_lock = asyncio.Lock()
    app.ctx.launcher = Launcher(
        app.ctx.registry,
        {"profiles": _pcfg["profiles"],
         "default_profile": _pcfg["default_profile"],
         "python": (config.get("agent") or {}).get("python")
         if isinstance(config.get("agent"), dict) else None},
        broker_port=port,
        token=app.ctx.auth_token,
    )
    if app.ctx.profiles_source == "sidecar":
        # Loud so a user hand-editing broker_config.json's agent.profiles and
        # seeing no change knows why: the sidecar shadows it (delete
        # webterm_profiles.json to revert to the broker_config seed).
        LOGGER.info("launch profiles: %d loaded from sidecar %s (broker_config "
                    "agent.profiles is the seed only)",
                    len(_pcfg["profiles"]), app.ctx.profiles_path)
    else:
        LOGGER.info("launch profiles: %d from broker_config/defaults (sidecar "
                    "%s not yet written)",
                    len(_pcfg["profiles"]), app.ctx.profiles_path)
    # Stable per-broker identity (#64): minted once into a sibling identity file,
    # immutable across restarts and OUTSIDE the rev cycle. Surfaced via /info so
    # the UI can detect the same broker reached through several URLs (the
    # duplicate-host-record bug) and gate the terminate fallback. Non-secret.
    app.ctx.broker_id = _load_or_create_broker_id(
        app.ctx.state_path.parent / "webterm_identity.json")
    LOGGER.info("broker identity: %s", app.ctx.broker_id)
    # Detached fire-and-forget tasks (e.g. the #33 MCP-activity pulse), held in
    # a set so they aren't GC'd mid-flight; each self-removes on completion.
    app.ctx.bg_tasks = set()
    # Single-active-browser lease (in-memory liveness, NOT persisted): the one
    # clientId allowed to drive this broker, the set of live /control sockets
    # per clientId, and the lock serializing every claim/release. Resets to
    # None on restart, so the first reconnecting /control auto-claims (the
    # lone-browser case needs no click). webterm_state.json is untouched.
    app.ctx.active_client_id = None
    app.ctx.control_clients = {}        # clientId -> set[ws]
    app.ctx.lease_lock = asyncio.Lock()
    # MCP HTTP interface runtime config (token + default mode + allow-launch +
    # master enable). Persisted in a sidecar beside the /state store (override
    # with "mcp_state_path"), seeded from config/env. The lock serializes the
    # read / mutate / atomic-write in POST /mcp/config (mirrors state_lock).
    app.ctx.mcp_state_path = Path(
        config.get("mcp_state_path")
        or (app.ctx.state_path.parent / "webterm_mcp.json")
    ).resolve()
    app.ctx.mcp_cfg = _load_mcp_cfg(app.ctx.mcp_state_path, config)
    app.ctx.mcp_lock = asyncio.Lock()
    _mc = app.ctx.mcp_cfg
    LOGGER.info("MCP interface: %s (default_mode=%s allow_launch=%s)",
                "enabled" if (_mc["enabled"] and _mc["token"]) else "disabled",
                _mc["default_mode"], _mc["allow_launch"])
    if app.ctx.auth_token:
        LOGGER.info("token auth enabled")
    else:
        LOGGER.warning("no auth token configured: /launch and non-loopback "
                       "producers are loopback-only/disabled")

    async def _cors_headers(request: Request, response):
        # Unconditional ACAO:* (see module docstring) — token-gating it left a
        # tokenless network-reachable broker unable to answer the UI's
        # cross-origin /sessions fetch. Sanic runs response middleware on error
        # paths too (401/404/405), which the cross-origin login probe depends
        # on — pinned by tests.
        response.headers["Access-Control-Allow-Origin"] = "*"
        if request.method == "OPTIONS":
            # PUT is for /state; GET/POST cover the rest.
            response.headers["Access-Control-Allow-Methods"] = \
                "GET, POST, PUT, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = \
                "Authorization, Content-Type"
            response.headers["Access-Control-Max-Age"] = "86400"
            # Chrome Private Network Access: a public-site page fetching a
            # private-network broker must see this echoed on the preflight.
            if request.headers.get(
                    "Access-Control-Request-Private-Network") == "true":
                response.headers["Access-Control-Allow-Private-Network"] = \
                    "true"

    app.register_middleware(_cors_headers, "response")

    async def _preflight(request: Request):
        # 204; the CORS response middleware decorates it.
        return empty()

    async def _browser_ws(request: Request, ws: Websocket):
        # In-handler post-upgrade auth: see module docstring (4401 vs 1006).
        token = app.ctx.auth_token
        if token and not auth.request_token_ok(request, token):
            LOGGER.warning("rejected unauthenticated /ws from %s", request.ip)
            await ws.close(code=4401, reason="auth required")
            return
        await relay.handle_browser_ws(request, ws, app.ctx.registry, app.ctx)

    async def _control_ws(request: Request, ws: Websocket):
        # Per-browser control channel for the single-active-browser lease.
        # Same post-upgrade auth gate as /ws (4401; token only when configured,
        # loopback/tailnet otherwise unauthenticated — same posture as /ws, and
        # /control can neither spawn nor mutate files so it adds no exposure).
        token = app.ctx.auth_token
        if token and not auth.request_token_ok(request, token):
            LOGGER.warning("rejected unauthenticated /control from %s",
                           request.ip)
            await ws.close(code=4401, reason="auth required")
            return
        client_id = (request.args.get("clientId") or "").strip()
        if not client_id:
            await ws.close(code=4400, reason="clientId required")
            return
        ctx = app.ctx

        async def _send(sock, owner_id):
            # Tell `sock` (opened by clientId `owner_id`) the CURRENT lease
            # status. Read live, never a captured snapshot: a status frame
            # queued behind an await must reflect the owner at send time, so a
            # become_active/release that linearized in between self-corrects
            # instead of leaving a client stuck on a stale owner. Every lease
            # transition re-notifies all affected sockets, so the final resting
            # status on each socket is always the truth.
            try:
                cur = ctx.active_client_id
                await sock.send(
                    protocol.control_status_frame(cur == owner_id, cur))
            except Exception as exc:
                LOGGER.debug("control send failed: %s", exc)

        # The connect registration is INSIDE the try so the finally always
        # runs (a cancellation during the initial _send must not strand this
        # ws in control_clients / pin the lease to a dead first client).
        try:
            # ---- connect: register + auto-activate-first ----------------
            async with ctx.lease_lock:
                ctx.control_clients.setdefault(client_id, set()).add(ws)
                if ctx.active_client_id is None:
                    ctx.active_client_id = client_id  # lone browser just works
            await _send(ws, client_id)

            async for message in ws:
                if not isinstance(message, str):
                    continue
                data = protocol.parse(message)
                if data is None or data.get("type") != "become_active":
                    continue
                # ---- become_active: flip the lease, then (OUTSIDE the lock)
                # cut every other client loose and broadcast live status.
                async with ctx.lease_lock:
                    ctx.active_client_id = client_id
                    losers = {cid: list(socks)
                              for cid, socks in ctx.control_clients.items()
                              if cid != client_id}
                    winners = list(ctx.control_clients.get(client_id, ()))
                for cid in losers:
                    await ctx.registry.close_clients_terminals(cid, 4409)
                for cid, socks in losers.items():
                    for lws in socks:
                        await _send(lws, cid)
                for wws in winners:
                    await _send(wws, client_id)
        except Exception as exc:
            LOGGER.info("control session ended: %s", exc)
        finally:
            # ---- disconnect: drop the ws; release the lease only if THIS
            # client held it and has no other live control socket. No
            # auto-promote — the remaining browsers keep their button.
            async with ctx.lease_lock:
                socks = ctx.control_clients.get(client_id)
                if socks is not None:
                    socks.discard(ws)
                    if not socks:
                        ctx.control_clients.pop(client_id, None)
                released = (ctx.active_client_id == client_id
                            and client_id not in ctx.control_clients)
                if released:
                    ctx.active_client_id = None
                    remaining = {cid: list(s)
                                 for cid, s in ctx.control_clients.items()}
                else:
                    remaining = {}
            for cid, socks in remaining.items():
                for rws in socks:
                    await _send(rws, cid)

    async def _producer_ws(request: Request, ws: Websocket):
        # Producers: loopback OR token (remote agents need the token; local
        # terminals/agents connect unchanged).
        if not auth.is_loopback_request(request):
            token = app.ctx.auth_token
            if not (token and auth.request_token_ok(request, token)):
                LOGGER.warning(
                    "rejected non-loopback /browserland from %s", request.ip)
                await ws.close(code=4401, reason="auth required")
                return
        await run_producer_session(ws, app.ctx.registry)

    async def _sessions(request: Request):
        token = app.ctx.auth_token
        if token and not auth.request_token_ok(request, token):
            return sanic_json({"ok": False, "error": "auth_required"},
                              status=401)
        # Stamp each summary's effective MCP mode so the UI's window menu can
        # tick the right radio off the existing 2s poll (no extra fetch).
        return sanic_json(app.ctx.registry.session_summaries(
            app.ctx.mcp_cfg["default_mode"]))

    async def _profiles(request: Request):
        # Same gate as /sessions. Names only — command/cwd never leave the
        # broker.
        token = app.ctx.auth_token
        if token and not auth.request_token_ok(request, token):
            return sanic_json({"ok": False, "error": "auth_required"},
                              status=401)
        return sanic_json({
            "default": app.ctx.launcher.default_profile,
            "profiles": sorted(app.ctx.launcher.profiles.keys()),
            # OS of this broker's host so the UI can pick the matching per-OS
            # default start path (issue #2). "windows" | "posix" — never a
            # path or anything host-identifying.
            "os": "windows" if os.name == "nt" else "posix",
        })

    def _parse_launch_body(request: Request):
        """Parse + validate a launch request body, shared by /launch and
        /mcp/launch. Returns ``(params, None)`` where params is the kwargs for
        ``launcher.launch``, or ``(None, error_response)``.

        ``cwd`` is the only client-supplied parameter that is more than dims/
        title — but it is DATA (the shell's cwd), never a command. It is
        validated as an existing directory and normalized to an absolute,
        symlink-collapsed path (rejecting the validate/spawn drift Codex
        flagged). Empty/missing -> the agent's default cwd. Not confined to a
        root: this is the broker's own host and a single-user token/loopback
        tool (a launch_root config could tighten it if ever bound wider)."""
        body: Dict[str, Any] = {}
        if request.body:
            try:
                parsed = json.loads(request.body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None, sanic_json({"ok": False, "error": "bad_json"},
                                        status=400)
            if isinstance(parsed, dict):
                body = parsed
            else:
                return None, sanic_json({"ok": False, "error": "bad_request"},
                                        status=400)
        try:
            cols = int(body.get("cols", 80))
            rows = int(body.get("rows", 24))
        except (TypeError, ValueError):
            return None, sanic_json({"ok": False, "error": "bad_dims"},
                                    status=400)
        title = body.get("title")
        if title is not None:
            title = str(title)[:256]
        cwd = body.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                return None, sanic_json({"ok": False, "error": "bad_cwd"},
                                        status=400)
            cwd = cwd.strip()
            if cwd:
                try:
                    cwd = os.path.realpath(cwd)
                except (OSError, ValueError):
                    return None, sanic_json({"ok": False, "error": "bad_cwd"},
                                            status=400)
                if not os.path.isdir(cwd):
                    return None, sanic_json({"ok": False,
                                             "error": "cwd_not_dir"},
                                            status=400)
            else:
                cwd = None
        return {"profile": body.get("profile"), "cols": cols, "rows": rows,
                "title": title, "cwd": cwd}, None

    async def _launch(request: Request):
        token = app.ctx.auth_token
        if token:
            if not auth.request_token_ok(request, token):
                LOGGER.warning("rejected unauthenticated /launch from %s",
                               request.ip)
                return sanic_json({"ok": False, "error": "auth_required"},
                                  status=401)
        elif not auth.is_loopback_request(request):
            LOGGER.warning("rejected non-loopback /launch from %s (no token "
                           "configured)", request.ip)
            return sanic_json(
                {"ok": False, "error": "launch_disabled_no_token"},
                status=403)

        params, err = _parse_launch_body(request)
        if err is not None:
            return err
        try:
            status, payload = await app.ctx.launcher.launch(
                params["profile"], cols=params["cols"], rows=params["rows"],
                title=params["title"], cwd=params["cwd"])
        except LaunchError as exc:
            return sanic_json(exc.payload, status=exc.status)
        return sanic_json(payload, status=status)

    # ---- token-or-loopback gate (shared by /file/* and /state) -----------
    # The EXACT /launch policy: token required when configured, else
    # loopback-only. CORS headers ride on every response via _cors_headers, so
    # a tokenless loopback broker stays unreadable cross-origin (same posture
    # — and same accepted single-user-loopback exposure — as /launch).
    def _gated_auth_error(request: Request, label: str):
        token = app.ctx.auth_token
        if token:
            if not auth.request_token_ok(request, token):
                LOGGER.warning("rejected unauthenticated %s from %s",
                               label, request.ip)
                return sanic_json({"ok": False, "error": "auth_required"},
                                  status=401)
        elif not auth.is_loopback_request(request):
            LOGGER.warning("rejected non-loopback %s from %s (no token "
                           "configured)", label, request.ip)
            return sanic_json(
                {"ok": False, "error": "disabled_no_token"}, status=403)
        return None

    def _file_auth_error(request: Request):
        return _gated_auth_error(request, "/file")

    async def _file_list(request: Request):
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        try:
            d = _resolve_host_path(str(body.get("path") or ""),
                                   app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(d)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "dir":
            return sanic_json({"ok": False, "error": "not_a_directory"},
                              status=400)
        entries = []
        try:
            for child in d.iterdir():
                try:
                    is_dir = child.is_dir()
                    size = 0 if is_dir else child.stat().st_size
                except OSError:
                    continue                       # unreadable entry — skip it
                entries.append({"name": child.name,
                                "type": "dir" if is_dir else "file",
                                "size": size})
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        # Dirs first, then case-insensitive by name.
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        # Host-wide (#35): cwd/parent are ABSOLUTE. parent is null only at a
        # filesystem anchor (``/``, ``C:\`` or ``\\srv\share``), where
        # ``d.parent == d`` — Up is inert there (no drive-list nav by design).
        parent = None if d.parent == d else str(d.parent)
        return sanic_json({
            "ok": True,
            "root": str(d.anchor),             # the FS anchor (informational)
            "cwd": str(d),
            "parent": parent,
            "entries": entries,
        })

    async def _file_read(request: Request):
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        try:
            with p.open("rb") as fh:
                # Read one byte past the cap: a file AT the cap still reads, but
                # anything larger is rejected — and reading bounded bytes (vs a
                # stat() then read_text()) closes the grow-after-check window
                # and caps how much we ever pull into memory.
                raw = fh.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        if len(raw) > MAX_FILE_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=400)
        # Binary-safe mode (#46): cross-host file transfer reads the SOURCE
        # broker's bytes as base64 (encoded HERE, server-side — the browser
        # never sees the raw bytes) and writes them to the DEST broker via
        # /file/upload. Gated on `b64 is True` (identity, not truthiness) so an
        # existing text caller that happens to carry a stray field can never
        # flip into binary mode and break its {content} contract.
        if body.get("b64") is True:
            return sanic_json({"ok": True,
                               "path": str(p),   # absolute, host-wide (#35)
                               "content_b64": base64.b64encode(raw)
                               .decode("ascii")})
        # #97: detect the common Windows/Mac/Linux text encodings (BOM-based for
        # multibyte) so a UTF-16/cp1252 file opens, and return the label so the
        # client can round-trip it on save. not_utf8 is kept for back-compat
        # (existing test + client copy); it now means "not supported text".
        try:
            content, encoding = _decode_file_text(raw)
        except _NotText:
            return sanic_json({"ok": False, "error": "not_utf8"}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p),       # absolute, host-wide (#35)
                           "content": content,
                           "encoding": encoding})

    async def _file_write(request: Request):
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        content = body.get("content")
        # #97: preserve the source encoding on save. None → utf-8 keeps existing
        # callers/tests (which send no encoding) writing plain UTF-8.
        encoding = body.get("encoding")
        if encoding is None:
            encoding = "utf-8"
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if not isinstance(content, str):
            return sanic_json({"ok": False, "error": "bad_content"},
                              status=400)
        if not isinstance(encoding, str) or encoding not in _TEXT_ENCODINGS:
            return sanic_json({"ok": False, "error": "bad_encoding"},
                              status=400)
        try:
            data = _encode_file_text(content, encoding)
        except UnicodeEncodeError:
            # Edited text gained a char the source encoding can't store; the
            # client prompts to re-save as UTF-8 (never a silent conversion).
            return sanic_json({"ok": False, "error": "encode_failed",
                               "encoding": encoding}, status=400)
        if len(data) > MAX_FILE_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=400)
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind not in ("file", "missing"):
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        parent = p.parent
        pkind = _classify_path(parent)
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        # Atomic write: temp file in the same dir, fsync-free os.replace swap
        # (atomic visibility — a reader never sees a half-written file; crash
        # durability is intentionally not guaranteed for an editor). The temp
        # is cleaned up on ANY failure (the finally covers a non-OSError too)
        # so a botched write never litters the tree; the fd is closed before
        # replace (Windows can't replace an open file).
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".webterm-",
                                       suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, str(p))
            tmp = None
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return sanic_json({"ok": True,
                           "path": str(p)})      # absolute, host-wide (#35)

    async def _file_upload(request: Request):
        # Binary-safe drop target (base64 content) — /file/write is UTF-8-text
        # only. Same host-wide resolution, atomic write, gate and cap as /file/write,
        # plus an `overwrite` flag (default false) so a drop never silently
        # clobbers an existing file (409 instead).
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        b64 = body.get("content_b64")
        overwrite = bool(body.get("overwrite", False))
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if not isinstance(b64, str):
            return sanic_json({"ok": False, "error": "bad_content"},
                              status=400)
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            return sanic_json({"ok": False, "error": "bad_base64"},
                              status=400)
        if len(data) > MAX_FILE_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=400)
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind in ("dir", "other"):
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        if kind == "file" and not overwrite:
            return sanic_json({"ok": False, "error": "exists"},
                              status=409)
        parent = p.parent
        pkind = _classify_path(parent)
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".webterm-",
                                       suffix=".tmp")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, str(p))
            tmp = None
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return sanic_json({"ok": True,
                           "path": str(p),       # absolute, host-wide (#35)
                           "size": len(data)})

    async def _file_delete(request: Request):
        # Destructive sibling of /file/write (#46), extended for the file manager
        # context menu (#72): a real directory is removed too, but only when the
        # caller passes recursive=true (a plain delete of a non-empty dir is a
        # 400 is_a_directory, so a mis-click can't wipe a tree).
        #
        # The headline correctness change (#72): the leaf is resolved BOTH ways.
        # p_leaf (link-safe) is checked for being a symlink/junction FIRST — if
        # so only the link ENTRY is removed, NEVER the target it points at. Only
        # a genuinely real path falls through to unlink (file) / rmtree (dir),
        # acting on the fully-resolved p. This closes the data-loss hole the old
        # ".resolve() then operate" path had (deleting a symlink-to-dir would
        # have rmtree'd the link's target tree, host-wide). No NEW privilege:
        # /file/write already grants full host-wide overwrite under this gate.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        recursive = bool(body.get("recursive", False))
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
            p_leaf = _resolve_host_path(rel, app.ctx.editor_root,
                                        follow_leaf=False)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        leaf_str = str(p_leaf)
        # lexists, not exists: a broken symlink (target gone) still has a link
        # entry that should be deletable, and a real link must be detected here
        # before any classification follows it.
        if not os.path.lexists(leaf_str):
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if _is_reparse_point(leaf_str):
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _remove_link, leaf_str)
            except (OSError, ValueError, shutil.Error, RecursionError) as exc:
                return sanic_json({"ok": False, "error": str(exc)}, status=400)
            return sanic_json({"ok": True, "path": leaf_str})
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind == "dir":
            if not recursive:
                return sanic_json({"ok": False, "error": "is_a_directory"},
                                  status=400)
            fn, arg = shutil.rmtree, str(p)
        elif kind == "file":
            fn, arg = os.unlink, str(p)
        else:
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        try:
            await asyncio.get_running_loop().run_in_executor(None, fn, arg)
        except (OSError, ValueError, shutil.Error, RecursionError) as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p)})      # absolute, host-wide (#35)

    # ---- richer file operations (#72) ------------------------------------
    # mkdir / copy / move / zip / unzip / stat round out the file manager's
    # context menu. Same token-or-loopback gate, host-wide resolution
    # (_resolve_host_path) and absolute-path echo as the read/write/delete
    # endpoints above; they add NO new privilege (an authenticated client
    # already has shell-level filesystem access). Heavy IO (copytree / rmtree /
    # zip / unzip) runs OFF the event loop via run_in_executor, and the catch is
    # broadened past OSError (shutil.Error, RecursionError, ValueError) so a
    # non-OSError failure still keeps the {ok:false,error} contract instead of
    # surfacing as a 500 + traceback.
    async def _file_mkdir(request: Request):
        # Create ONE directory. os.mkdir (NOT makedirs) — the parent must
        # already be a dir (parent_missing else), so a typo can't silently
        # build a chain of dirs. An existing path is a 409 conflict.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind != "missing":
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        parent = p.parent
        pkind = _classify_path(parent)
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, os.mkdir, str(p))
        except (OSError, ValueError, shutil.Error, RecursionError) as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p)})      # absolute, host-wide (#35)

    async def _file_copy(request: Request):
        # Copy a file or directory tree. src is followed (copy is non-destructive
        # to the source); dst is the FULL target path (not a container). A dir
        # uses copytree(symlinks=True) so INNER links are copied as links, not
        # materialised; a file uses copy2 (metadata preserved). Refuses src==dst
        # and dst-inside-src (the latter would recurse / litter — P0-2).
        #
        # NOTE: an overwrite is NOT atomic — copy2 / copytree(dirs_exist_ok=True)
        # write over the destination in place, so a mid-copy failure can leave a
        # damaged dst the caller asked to replace. Partial-dst cleanup therefore
        # runs only when !overwrite (where dst was freshly created and is ours to
        # remove); an overwrite failure is reported, not rolled back.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        overwrite = bool(body.get("overwrite", False))
        try:
            src, dst = _resolve_two(body, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if src == dst:
            return sanic_json({"ok": False, "error": "same"}, status=400)
        if _is_within(dst, src):
            return sanic_json({"ok": False, "error": "dest_in_source"},
                              status=400)
        src_kind = _classify_path(src)
        if src_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if src_kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if src_kind not in ("file", "dir"):
            return sanic_json({"ok": False, "error": "not_supported"},
                              status=400)
        dst_kind = _classify_path(dst)
        if dst_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        dparent = _classify_path(dst.parent)
        if dparent == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dparent != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        if dst_kind != "missing":
            if not overwrite:
                return sanic_json({"ok": False, "error": "exists"}, status=409)
            if dst_kind != src_kind:
                return sanic_json({"ok": False, "error": "type_mismatch"},
                                  status=400)
        src_str, dst_str = str(src), str(dst)

        def _do_copy():
            if src_kind == "dir":
                shutil.copytree(src_str, dst_str, symlinks=True,
                                dirs_exist_ok=overwrite)
            else:
                shutil.copy2(src_str, dst_str)

        try:
            await asyncio.get_running_loop().run_in_executor(None, _do_copy)
        except (OSError, ValueError, shutil.Error, RecursionError) as exc:
            if not overwrite:
                # dst was freshly created by this op — remove the partial litter.
                try:
                    if os.path.isdir(dst_str) and not _is_reparse_point(dst_str):
                        shutil.rmtree(dst_str, ignore_errors=True)
                    elif os.path.lexists(dst_str):
                        os.unlink(dst_str)
                except OSError:
                    pass
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True, "path": dst_str})

    async def _file_move(request: Request):
        # Move/rename a file or directory. Both paths resolve LINK-SAFE (#72): a
        # symlink/junction src relocates the LINK entry, never its target tree.
        # dst is the FULL target path. An existing dst needs overwrite=true (409
        # otherwise). Overwrite never loses the old dst: two real files use the
        # atomic os.replace; anything else (dir, symlink/junction, type change)
        # renames the existing dst to a sibling backup, moves src into place, and
        # only then drops the backup — restoring it if the move fails.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        overwrite = bool(body.get("overwrite", False))
        try:
            src, dst = _resolve_two(body, app.ctx.editor_root,
                                    src_follow_leaf=False, dst_follow_leaf=False)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if src == dst:
            return sanic_json({"ok": False, "error": "same"}, status=400)
        if _is_within(dst, src):
            return sanic_json({"ok": False, "error": "dest_in_source"},
                              status=400)
        src_str, dst_str = str(src), str(dst)
        # lexists (not exists): a broken symlink leaf still exists and must be
        # movable; a real symlink/junction must not be dereferenced here.
        if not os.path.lexists(src_str):
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        dparent = _classify_path(dst.parent)
        if dparent == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dparent != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        dst_exists = os.path.lexists(dst_str)
        if dst_exists and not overwrite:
            return sanic_json({"ok": False, "error": "exists"}, status=409)

        def _do_move():
            if not (dst_exists and overwrite):
                _rename_or_move(src_str, dst_str)
                return
            both_real_files = (
                os.path.isfile(src_str) and not _is_reparse_point(src_str)
                and os.path.isfile(dst_str) and not _is_reparse_point(dst_str))
            if both_real_files:
                _rename_or_move(src_str, dst_str)   # atomic file replace
                return
            # No atomic replace for these (no dir-over-dir replace on Windows);
            # back up the existing dst, move, restore on ANY failure so dst is
            # never lost.
            backup = dst_str + ".webterm-bak-" + uuid.uuid4().hex
            os.rename(dst_str, backup)
            try:
                _rename_or_move(src_str, dst_str)
            except BaseException:
                try:
                    if os.path.lexists(dst_str):
                        _force_remove(dst_str)
                except OSError:
                    pass
                try:
                    os.rename(backup, dst_str)
                except OSError:
                    pass
                raise
            _force_remove(backup)

        try:
            await asyncio.get_running_loop().run_in_executor(None, _do_move)
        except (OSError, ValueError, shutil.Error, RecursionError) as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True, "path": dst_str})

    async def _file_zip(request: Request):
        # Create a .zip from a file or directory tree. dest is the output archive
        # path (its parent must be a dir). A pre-scan rejects a source that
        # exceeds the caps BEFORE writing; the archive is built into a tempfile
        # in dest's parent and os.replace'd into place, so dest is never left
        # partial and an overwrite keeps the old archive until the new one is
        # complete. Reparse-point subdirectories (symlinks AND junctions) are NOT
        # followed (filtered out of the walk, so they're omitted); symlinked
        # FILES are archived as their target content (zf.write follows them, and
        # the pre-scan counts the same target size via getsize). The caps are
        # pre-scan advisory — single-user, the source is the caller's own tree.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        overwrite = bool(body.get("overwrite", False))
        src_rel = body.get("src")
        dest_rel = body.get("dest")
        if not isinstance(src_rel, str) or not src_rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if not isinstance(dest_rel, str) or not dest_rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        try:
            src = _resolve_host_path(src_rel, app.ctx.editor_root)
            dest = _resolve_host_path(dest_rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        src_kind = _classify_path(src)
        if src_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if src_kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if src_kind not in ("file", "dir"):
            return sanic_json({"ok": False, "error": "not_supported"},
                              status=400)
        dest_kind = _classify_path(dest)
        if dest_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dest_kind == "dir":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)
        if dest_kind != "missing" and not overwrite:
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        dparent = _classify_path(dest.parent)
        if dparent == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dparent != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        if src_kind == "dir" and _is_within(dest, src):
            # The growing archive must not live inside the tree being zipped.
            return sanic_json({"ok": False, "error": "dest_in_source"},
                              status=400)
        src_str, dest_str = str(src), str(dest)

        def _do_zip():
            total = 0
            count = 0
            if src_kind == "file":
                total = os.path.getsize(src_str)
                count = 1
            else:
                for root, dirs, files in os.walk(src_str):
                    dirs[:] = [d for d in dirs
                               if not _is_reparse_point(os.path.join(root, d))]
                    count += 1 + len(files)        # this dir entry + its files
                    if count > MAX_ARCHIVE_ENTRIES:
                        raise ValueError("too_many_entries")
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(root, name))
                        except OSError:
                            pass
                    if total > MAX_ARCHIVE_BYTES:
                        raise ValueError("archive_too_large")
            parent = os.path.dirname(dest_str) or "."
            fd, tmp = tempfile.mkstemp(dir=parent, prefix=".webterm-zip-",
                                       suffix=".tmp")
            os.close(fd)
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    if src_kind == "file":
                        zf.write(src_str, arcname=os.path.basename(src_str))
                    else:
                        # arcnames relative to src's PARENT so the archive holds
                        # the top folder; write each walked dir (preserves empty
                        # dirs) then each file.
                        base = os.path.dirname(src_str)
                        for root, dirs, files in os.walk(src_str):
                            dirs[:] = [d for d in dirs
                                       if not _is_reparse_point(
                                           os.path.join(root, d))]
                            zf.write(root, arcname=os.path.relpath(root, base))
                            for name in files:
                                fp = os.path.join(root, name)
                                zf.write(fp, arcname=os.path.relpath(fp, base))
                os.replace(tmp, dest_str)
                tmp = None
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        try:
            await asyncio.get_running_loop().run_in_executor(None, _do_zip)
        except (OSError, ValueError, shutil.Error, RecursionError,
                zipfile.BadZipFile) as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True, "path": dest_str})

    async def _file_unzip(request: Request):
        # Extract a .zip into a FRESH dest directory (dest must not exist). A
        # zip-bomb / oversize guard rejects an archive whose entry count or
        # cumulative declared uncompressed size exceeds the caps BEFORE any
        # extraction. CPython's extractall already neutralises path traversal
        # (absolute / drive-letter / '..' members are sanitised to land UNDER
        # dest), so there is deliberately no hand-rolled commonpath loop; a
        # malformed member fails extraction cleanly and the freshly-created dest
        # is removed. The size guard trusts the central-directory sizes
        # (single-user threat model).
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        path_rel = body.get("path")
        dest_rel = body.get("dest")
        if not isinstance(path_rel, str) or not path_rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if not isinstance(dest_rel, str) or not dest_rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        try:
            zpath = _resolve_host_path(path_rel, app.ctx.editor_root)
            dest = _resolve_host_path(dest_rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        zkind = _classify_path(zpath)
        if zkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if zkind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if zkind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)
        dest_kind = _classify_path(dest)
        if dest_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dest_kind != "missing":
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        dparent = _classify_path(dest.parent)
        if dparent == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dparent != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        zpath_str, dest_str = str(zpath), str(dest)

        def _do_unzip():
            with zipfile.ZipFile(zpath_str) as zf:
                infos = zf.infolist()
                if len(infos) > MAX_ARCHIVE_ENTRIES:
                    raise ValueError("too_many_entries")
                if sum(zi.file_size for zi in infos) > MAX_ARCHIVE_BYTES:
                    raise ValueError("archive_too_large")
                os.mkdir(dest_str)
                try:
                    zf.extractall(dest_str)
                except BaseException:
                    shutil.rmtree(dest_str, ignore_errors=True)
                    raise

        try:
            await asyncio.get_running_loop().run_in_executor(None, _do_unzip)
        except zipfile.BadZipFile:
            return sanic_json({"ok": False, "error": "bad_zip"}, status=400)
        except (OSError, ValueError, shutil.Error, RecursionError) as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True, "path": dest_str})

    async def _file_stat(request: Request):
        # Properties (#72): type/size/mtime/mode for one path, plus a shallow
        # child count for a directory. Read-only and chosen over extending
        # /file/list (which only describes a dir's CHILDREN and is the hot path
        # behind openFileDialog/renderPane). mtime is epoch seconds; mode is the
        # raw st_mode int (the UI formats both).
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        try:
            st = p.stat()
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        out = {"ok": True, "path": str(p), "type": kind,
               "size": st.st_size, "mtime": st.st_mtime, "mode": st.st_mode,
               "os": "windows" if os.name == "nt" else "posix"}        # #96
        if os.name == "nt":
            # Windows attr breakdown from the already-acquired st (no extra
            # syscall). POSIX rwx is derivable client-side from `mode`, so it
            # needs nothing here. FILE_ATTRIBUTE_* exist on all platforms but
            # are only referenced under this nt guard (mirrors _is_reparse_point).
            attrs = getattr(st, "st_file_attributes", 0)
            out["attributes"] = {
                "readonly": bool(attrs & stat.FILE_ATTRIBUTE_READONLY),
                "hidden":   bool(attrs & stat.FILE_ATTRIBUTE_HIDDEN),
                "archive":  bool(attrs & stat.FILE_ATTRIBUTE_ARCHIVE),
            }
        if kind == "dir":
            try:
                out["children"] = sum(1 for _ in p.iterdir())
            except OSError:
                pass                               # unreadable dir — omit count
        return sanic_json(out)

    async def _file_setattr(request: Request):
        # Editable Properties (#96): flip Windows READONLY/HIDDEN/ARCHIVE or
        # POSIX rwx on ONE path — the mutating sibling of /file/stat. follow_leaf
        # stays True (the default) ON PURPOSE: operate on the TARGET the dialog
        # showed, the opposite of move/delete which preserve a leaf link (S4).
        # The branch is chosen from the broker's OWN os.name; we never infer the
        # host OS from the payload shape (S1).
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:        # N3
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        loop = asyncio.get_running_loop()
        if os.name == "nt":
            attributes = body.get("attributes")
            if not isinstance(attributes, dict):
                return sanic_json({"ok": False, "error": "bad_attrs"},
                                  status=400)
            try:
                await loop.run_in_executor(
                    None, _set_windows_attributes, str(p), attributes)
            except OSError as exc:                  # N1/N2: Win32 / long-path
                return sanic_json({"ok": False, "error": str(exc)}, status=400)
        else:
            mode = body.get("mode")
            # A non-int (or bool, an int subclass) would make os.chmod raise
            # TypeError — NOT in the catch tuple — and escape as a 500 (C2).
            if (not isinstance(mode, int) or isinstance(mode, bool)
                    or not 0 <= mode <= 0o7777):
                return sanic_json({"ok": False, "error": "bad_mode"},
                                  status=400)

            def _chmod():
                # Preserve special bits (setuid/setgid/sticky) SERVER-SIDE from a
                # live re-stat, never the client (C3): only the low 9 perm bits
                # come from the request.
                live = os.stat(str(p)).st_mode
                os.chmod(str(p), (mode & 0o777) | (live & 0o7000))

            try:
                await loop.run_in_executor(None, _chmod)
            except OSError as exc:                  # not-owner PermissionError, …
                return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True, "path": str(p)})

    # ---- chunked transfer (#108) -----------------------------------------
    # Lift the 5 MiB whole-file cap for the two BYTE paths — cross-host copy/move
    # and in-app download — by streaming a file in bounded chunks. /file/read_chunk
    # is a ranged read; the /file/upload_* trio is an append-and-atomic-replace
    # upload session. All POST with the SAME auth gate as every other /file/* route
    # (so the route-enumeration auth test covers them unchanged), and none is bound
    # by MAX_FILE_BYTES. The editor's careful capped whole-file /file/read is left
    # untouched — a dedicated ranged endpoint keeps that regression surface at nil.
    async def _file_read_chunk(request: Request):
        # Ranged read: seek(offset), read up to min(length, MAX_CHUNK_BYTES). The
        # response carries the DECODED chunk length so the client advances offset
        # by real bytes — never by the total size or the base64 string length.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        offset = body.get("offset", 0)
        length = body.get("length", MAX_CHUNK_BYTES)
        # Strict ints (bool is an int subclass — exclude it) so a bad range is a
        # clean 400, never a seek/read TypeError surfacing as a 500.
        if (isinstance(offset, bool) or isinstance(length, bool)
                or not isinstance(offset, int) or not isinstance(length, int)
                or offset < 0 or length < 1):
            return sanic_json({"ok": False, "error": "bad_range"}, status=400)
        length = min(length, MAX_CHUNK_BYTES)   # never read more than one chunk
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)
        try:
            # stat per call so eof reflects the CURRENT size (best-effort live
            # read; a file that grows/shrinks mid-stream converges each round —
            # #110 adds a checksum for integrity, out of scope here).
            size = p.stat().st_size
            with p.open("rb") as fh:
                fh.seek(offset)
                raw = fh.read(length)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({
            "ok": True,
            "path": str(p),                 # absolute, host-wide (#35)
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "length": len(raw),             # decoded bytes in THIS chunk
            "size": size,                   # total file size
            "offset": offset,
            "eof": offset + len(raw) >= size,
        })

    async def _file_upload_begin(request: Request):
        # Open an upload session at ``path``: validate the dest like /file/upload
        # (parent is a dir; existing dir -> is_dir; existing file needs overwrite),
        # then mkstemp a .part file IN THE DEST PARENT so commit's os.replace is an
        # atomic same-filesystem swap. follow_leaf=False (like move/delete): commit
        # replaces a symlink/junction leaf as the ENTRY, never through to its target.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        overwrite = bool(body.get("overwrite", False))
        try:
            p = _resolve_host_path(rel, app.ctx.editor_root, follow_leaf=False)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        parent = p.parent
        pkind = _classify_path(parent)
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        kind = _classify_path(p)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "dir":
            return sanic_json({"ok": False, "error": "is_dir"}, status=400)
        if kind == "other":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)
        if kind == "file" and not overwrite:
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        # Sweep BEFORE the cap check so abandoned sessions free their slot, and
        # drop crash-orphaned temps in the dir we're about to write.
        now = time.time()
        _sweep_upload_sessions(app.ctx.uploads, now)
        _sweep_orphan_parts(parent, now)
        if len(app.ctx.uploads) >= MAX_UPLOAD_SESSIONS:
            return sanic_json({"ok": False, "error": "too_many_sessions"},
                              status=429)
        try:
            fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=".webterm-up-",
                                       suffix=".part")
            os.close(fd)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        upload_id = secrets.token_hex(16)
        app.ctx.uploads[upload_id] = {
            "tmp": tmp, "dest": str(p), "overwrite": overwrite,
            "received": 0, "created": now,
        }
        return sanic_json({"ok": True, "upload_id": upload_id})

    async def _file_upload_chunk(request: Request):
        # Append ONE chunk to an open session. Rejects (without appending) a
        # missing session, bad base64, an oversized decoded chunk, an out-of-order
        # offset, and a chunk that would push the session past MAX_TRANSFER_BYTES.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        upload_id = body.get("upload_id")
        b64 = body.get("content_b64")
        offset = body.get("offset", 0)
        if not isinstance(upload_id, str) or not isinstance(b64, str):
            return sanic_json({"ok": False, "error": "bad_request"}, status=400)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return sanic_json({"ok": False, "error": "bad_offset"}, status=400)
        session = app.ctx.uploads.get(upload_id)
        if session is None:
            return sanic_json({"ok": False, "error": "no_session"}, status=404)
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            return sanic_json({"ok": False, "error": "bad_base64"}, status=400)
        if len(data) > MAX_CHUNK_BYTES:
            return sanic_json({"ok": False, "error": "chunk_too_large"},
                              status=400)
        # Ordering guard: the client streams sequentially, so this chunk must
        # start exactly where the last ended. A gap/dup/reorder is rejected
        # WITHOUT appending (never silently corrupts the temp).
        if offset != session["received"]:
            return sanic_json({"ok": False, "error": "bad_offset",
                               "received": session["received"]}, status=409)
        if session["received"] + len(data) > MAX_TRANSFER_BYTES:
            # Past the per-session ceiling: drop the whole session (temp + slot)
            # so a runaway transfer can't keep consuming disk.
            app.ctx.uploads.pop(upload_id, None)
            try:
                os.unlink(session["tmp"])
            except OSError:
                pass
            return sanic_json({"ok": False, "error": "too_large"}, status=400)
        try:
            with open(session["tmp"], "ab") as fh:
                fh.write(data)
        except OSError as exc:
            # A failed append leaves the temp in an unknown state — drop the
            # session so the client can never commit a corrupt file.
            app.ctx.uploads.pop(upload_id, None)
            try:
                os.unlink(session["tmp"])
            except OSError:
                pass
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        session["received"] += len(data)       # only after a successful write
        return sanic_json({"ok": True, "received": session["received"]})

    async def _file_upload_commit(request: Request):
        # Finalize: atomically os.replace the temp onto the dest. Re-checks the
        # exists race unless overwriting. On any failure the temp + session are
        # dropped (nothing leaks). (#110 will verify a SHA-256 here first.)
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        upload_id = body.get("upload_id")
        if not isinstance(upload_id, str):
            return sanic_json({"ok": False, "error": "bad_request"}, status=400)
        session = app.ctx.uploads.get(upload_id)
        if session is None:
            return sanic_json({"ok": False, "error": "no_session"}, status=404)
        dest, tmp = session["dest"], session["tmp"]
        if not session["overwrite"] and os.path.lexists(dest):
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        try:
            size = os.path.getsize(tmp)
            os.replace(tmp, dest)
        except OSError as exc:
            # replace-over-dir (dest turned into a dir since begin) or any IO
            # error: drop temp + session, report a clear code.
            app.ctx.uploads.pop(upload_id, None)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            code = "is_dir" if os.path.isdir(dest) else str(exc)
            return sanic_json({"ok": False, "error": code}, status=400)
        app.ctx.uploads.pop(upload_id, None)
        return sanic_json({"ok": True, "path": dest, "size": size})

    async def _file_upload_abort(request: Request):
        # Idempotent best-effort teardown: pop the session + unlink its temp.
        # An already-gone session (e.g. a disposal abort racing a completed
        # commit) still returns {ok:true}, so the client treats abort purely as
        # cleanup and always reports the ORIGINAL failure.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        upload_id = body.get("upload_id")
        if not isinstance(upload_id, str):
            return sanic_json({"ok": False, "error": "bad_request"}, status=400)
        session = app.ctx.uploads.pop(upload_id, None)
        if session is not None:
            try:
                os.unlink(session["tmp"])
            except OSError:
                pass
        return sanic_json({"ok": True})

    @app.before_server_stop
    async def _drain_upload_sessions(app_, loop):
        # Unlink every in-flight upload temp on shutdown so a restart doesn't
        # leave .webterm-up-*.part litter (the lazy begin-sweep only runs while
        # the broker is up). Best-effort; the dict is cleared regardless.
        for session in list(app_.ctx.uploads.values()):
            try:
                os.unlink(session["tmp"])
            except OSError:
                pass
        app_.ctx.uploads.clear()

    # ---- task manager + git button (/session/*) --------------------------
    # On-demand broker<->producer round-trips (correlated by req id) so process
    # listing, scoped kill, and git status work for LOCAL and REMOTE sessions
    # alike (the agent does the work in its own host/cwd). Same token-or-loopback
    # gate as /launch — killing processes is privileged. The agent scopes kills
    # to the session's own process tree; the broker never trusts a client pid
    # beyond relaying it.
    async def _session_rpc(entry, make_frame, expected: str,
                           timeout: float = RPC_TIMEOUT):
        """Park a Future on the producer entry, send the request frame, await
        the matching reply. Returns ``(payload, error)`` where error is one of
        None / "busy" / "timeout" / "gone". ``timeout`` is extended for a
        read_screen wait-for-change so the RPC outlives the agent's wait (#26)."""
        allocated = entry.new_rpc(expected)
        if allocated is None:
            return None, "busy"          # too many in flight on this session
        req, future = allocated
        try:
            await entry.send_to_producer(make_frame(req))
            payload = await asyncio.wait_for(future, timeout=timeout)
            return payload, None
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception:
            # Producer disconnected/replaced -> fail_all_rpc set an exception.
            return None, "gone"
        finally:
            entry.cancel_rpc(req, future)

    def _session_entry(request):
        """Resolve the {id} body to a live producer entry, or return an error
        response. Returns ``(entry, None)`` or ``(None, response)``."""
        body = _json_object_body(request)
        if body is None:
            return None, sanic_json({"ok": False, "error": "bad_json"},
                                    status=400)
        try:
            sid = int(body.get("id"))
        except (TypeError, ValueError):
            return None, sanic_json({"ok": False, "error": "bad_id"},
                                    status=400)
        entry = app.ctx.registry.get(sid)
        if entry is None:
            return None, sanic_json({"ok": False, "error": "unknown_session"},
                                    status=404)
        return (entry, body), None

    def _rpc_error_response(error: str):
        if error == "busy":
            return sanic_json({"ok": False, "error": "busy"}, status=429)
        if error == "timeout":
            return sanic_json({"ok": False, "error": "timeout"}, status=504)
        return sanic_json({"ok": False, "error": "session_gone"}, status=409)

    async def _session_procs(request: Request):
        err = _gated_auth_error(request, "/session/procs")
        if err is not None:
            return err
        resolved, resp = _session_entry(request)
        if resp is not None:
            return resp
        entry, _body = resolved
        payload, error = await _session_rpc(
            entry, protocol.procs_please_frame, "procs")
        if error is not None:
            return _rpc_error_response(error)
        procs = (payload or {}).get("procs") or []
        return sanic_json({"ok": True, "procs": procs})

    async def _session_kill(request: Request):
        err = _gated_auth_error(request, "/session/kill")
        if err is not None:
            return err
        resolved, resp = _session_entry(request)
        if resp is not None:
            return resp
        entry, body = resolved
        try:
            pid = int(body.get("pid"))
        except (TypeError, ValueError):
            return sanic_json({"ok": False, "error": "bad_pid"}, status=400)
        payload, error = await _session_rpc(
            entry, lambda req: protocol.kill_frame(req, pid), "killed")
        if error is not None:
            return _rpc_error_response(error)
        payload = payload or {}
        return sanic_json({"ok": bool(payload.get("ok")),
                           "error": payload.get("error"),
                           "pid": pid})

    async def _session_git(request: Request):
        err = _gated_auth_error(request, "/session/git")
        if err is not None:
            return err
        resolved, resp = _session_entry(request)
        if resp is not None:
            return resp
        entry, _body = resolved
        payload, error = await _session_rpc(
            entry, protocol.git_status_please_frame, "git_status")
        if error is not None:
            return _rpc_error_response(error)
        # Pass the agent's status dict through (minus the protocol envelope).
        payload = dict(payload or {})
        payload.pop("type", None)
        payload.pop("req", None)
        payload.setdefault("ok", False)
        return sanic_json(payload)

    async def _session_mcp(request: Request):
        # Browser-facing per-window MCP-mode setter. Gated by the BROWSER
        # auth_token (this is the UI editing policy), NOT the MCP token. Sets
        # the in-memory per-window override; None default = inherit the broker
        # default. Resets on broker restart / agent relaunch by design.
        err = _gated_auth_error(request, "/session/mcp")
        if err is not None:
            return err
        resolved, resp = _session_entry(request)
        if resp is not None:
            return resp
        entry, body = resolved
        mode = body.get("mode")
        if mode not in MCP_MODES:
            return sanic_json({"ok": False, "error": "bad_mode"}, status=400)
        entry.mcp_mode = mode
        return sanic_json({"ok": True, "id": entry.id, "mode": mode})

    # ---- MCP HTTP interface (/mcp/*) -------------------------------------
    # Consumed by an EXTERNAL MCP server against a documented contract. Gated
    # by the per-broker MCP token only — NO loopback exemption (unlike
    # auth_token): MCP is opt-in, so with no token configured (or the feature
    # disabled) the whole surface is 403 mcp_disabled. CORS rides the shared
    # response middleware; OPTIONS preflights are registered alongside.
    def _mcp_auth_error(request: Request):
        cfg = app.ctx.mcp_cfg
        if not cfg.get("enabled") or not cfg.get("token"):
            return sanic_json({"error": "mcp_disabled"}, status=403)
        if not auth.request_token_ok(request, cfg["token"]):
            LOGGER.warning("rejected unauthenticated /mcp from %s", request.ip)
            return sanic_json({"error": "auth_required"}, status=401)
        return None

    def _mcp_effective_mode(entry) -> str:
        # Per-window override OR the live broker default, so flipping the
        # default live-updates every non-overridden window.
        return entry.mcp_mode or app.ctx.mcp_cfg["default_mode"]

    def _mcp_entry(request: Request):
        """Resolve {id} to a live entry whose effective mode != off. Returns
        ``(entry, mode, None)`` or ``(None, None, error_response)`` (404
        unknown_or_off / 400 bad body)."""
        body = _json_object_body(request)
        if body is None:
            return None, None, sanic_json({"error": "bad_json"}, status=400)
        try:
            sid = int(body.get("id"))
        except (TypeError, ValueError):
            return None, None, sanic_json({"error": "bad_id"}, status=400)
        entry = app.ctx.registry.get(sid)
        mode = _mcp_effective_mode(entry) if entry is not None else "off"
        if entry is None or mode == "off":
            return None, None, sanic_json({"error": "unknown_or_off"},
                                          status=404)
        return entry, mode, None

    async def _mcp_info(request: Request):
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        cfg = app.ctx.mcp_cfg
        return sanic_json({"ok": True,
                           "allow_launch": bool(cfg["allow_launch"]),
                           "default_mode": cfg["default_mode"],
                           "version": app.ctx.version})

    async def _mcp_terminals(request: Request):
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        default_mode = app.ctx.mcp_cfg["default_mode"]
        broker_version = app.ctx.version
        out = []
        for s in app.ctx.registry.session_summaries(default_mode):
            mode = s.get("mcp", "off")
            if mode == "off":
                continue
            version = s.get("version", "") or ""
            entry_out = {"id": s["id"], "title": s["title"], "host": s["host"],
                         "cwd": s["cwd"], "agent": s["agent"], "kind": s["kind"],
                         "cols": s["cols"], "rows": s["rows"], "mode": mode,
                         "version": version,
                         # DECCKM, cached from the agent's `mode` pushes (#23);
                         # send_keys reads it to pick CSI vs SS3 arrows.
                         "app_cursor": bool(s.get("app_cursor", False))}
            # ``stale`` = this producer's build differs from the broker's (incl. a
            # pre-#22 agent reporting no version) → a deploy predating a fix, so a
            # client can warn without comparing strings (#22). Only meaningful for
            # webterm AGENT producers (a non-agent terminal legitimately reports
            # no version — flagging it stale would be noise), and reliable only
            # when builds carry a git hash (see build_version()).
            if s["kind"] == "agent":
                entry_out["stale"] = version != broker_version
            out.append(entry_out)
        return sanic_json(out)

    def _flash_mcp_activity(entry, kind: str) -> None:
        # #33: emit a per-window MCP-activity pulse (browser flashes the robot
        # icon — cool/soft for a read, warm/sharp for a write). Scheduled as a
        # DETACHED task (not awaited) so the agent's read/write response latency
        # never couples to a slow/backpressured browser WS send; broadcast_text
        # swallows per-subscriber errors and no-ops with no subscribers.
        task = asyncio.ensure_future(
            entry.broadcast_text(protocol.mcp_activity_frame(kind)))
        app.ctx.bg_tasks.add(task)
        task.add_done_callback(app.ctx.bg_tasks.discard)

    async def _mcp_read(request: Request):
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        entry, _mode, resp = _mcp_entry(request)
        if resp is not None:
            return resp
        _flash_mcp_activity(entry, "read")    # #33 (detached; see helper)
        # Same correlated round-trip as /session/procs. A non-agent
        # (terminal) producer has no screen handler -> the RPC times out ->
        # 502 no_producer_rpc. busy/gone collapse to the same: there is no
        # producer that can answer right now.
        # #21: optional view/lines for scrollback. Hard-cap lines here; the
        # agent budgets history further (lines AND total cells).
        body = _json_object_body(request) or {}
        view = "scrollback" if body.get("view") == "scrollback" else "screen"
        try:
            lines = int(body.get("lines", 0) or 0)
        except (TypeError, ValueError):
            lines = 0
        lines = max(0, min(lines, 1000))
        # wait-for-change (#26): a prior content_hash + a timeout. The agent
        # holds the reply until the screen hash differs or timeout_ms elapses;
        # extend the RPC timeout to outlive that wait (plus RPC_TIMEOUT of slack
        # for dispatch + the final render). Without a baseline hash this is a
        # plain immediate read on the default timeout.
        wait_for_change = body.get("wait_for_change")
        if not isinstance(wait_for_change, str) or not wait_for_change:
            wait_for_change = None
        # wait-for-content (#51): a substring or regex predicate, same timeout.
        # Validate the regex HERE so a bad pattern fails fast with a clean 400
        # instead of the agent waiting out the whole timeout and returning
        # matched=false.
        wait_for_text = body.get("wait_for_text")
        if not isinstance(wait_for_text, str) or not wait_for_text:
            wait_for_text = None
        wait_for_regex = body.get("wait_for_regex")
        if not isinstance(wait_for_regex, str) or not wait_for_regex:
            wait_for_regex = None
        if wait_for_regex is not None:
            try:
                re.compile(wait_for_regex)
            except re.error as exc:
                return sanic_json({"error": "bad_regex", "detail": str(exc)},
                                  status=400)
        # The wait modes are exclusive (#51): wait_for_change, wait_for_text and
        # wait_for_regex each pick a different signal, and combining them has no
        # well-defined meaning (which one decides `matched`?). Reject up front so
        # a caller never gets a silently-wrong wait.
        n_wait = sum(bool(x) for x in
                     (wait_for_change, wait_for_text, wait_for_regex))
        if n_wait > 1:
            return sanic_json(
                {"error": "conflicting_wait",
                 "detail": "use only one of wait_for_change / wait_for_text / "
                           "wait_for_regex"}, status=400)
        wait_absent = bool(body.get("wait_absent", False))
        # delta (#52): a prior content_hash; the agent returns only changed rows
        # since that frame when it can, else a full grid. Orthogonal to the wait
        # modes (it shapes the reply, not when it fires), so it does not count
        # toward the conflicting_wait check above.
        since = body.get("since")
        if not isinstance(since, str) or not since:
            since = None
        try:
            timeout_ms = int(body.get("timeout_ms", 0) or 0)
        except (TypeError, ValueError):
            timeout_ms = 0
        timeout_ms = max(0, min(timeout_ms, MAX_MCP_WAIT_MS))
        rpc_timeout = RPC_TIMEOUT
        if wait_for_change or wait_for_text or wait_for_regex:
            rpc_timeout = RPC_TIMEOUT + timeout_ms / 1000.0
        payload, error = await _session_rpc(
            entry,
            lambda req: protocol.screen_text_please_frame(
                req, view, lines, wait_for_change, timeout_ms,
                wait_for_text=wait_for_text, wait_for_regex=wait_for_regex,
                wait_absent=wait_absent, since=since),
            "screen_text", timeout=rpc_timeout)
        if error is not None:
            return sanic_json({"error": "no_producer_rpc"}, status=502)
        payload = payload or {}
        out = {"ok": True, "id": entry.id,
               "cols": payload.get("cols", entry.cols),
               "rows": payload.get("rows", entry.rows),
               "text": payload.get("text", ""),
               # New fields (#21/#23/#26); older agents omit them -> defaults.
               "alt_screen": bool(payload.get("alt_screen", False)),
               "app_cursor": bool(payload.get("app_cursor", False)),
               "view": payload.get("view", "screen"),
               "history_lines": int(payload.get("history_lines", 0) or 0),
               "content_hash": str(payload.get("content_hash", "") or ""),
               "cursor": payload.get("cursor")}
        # matched (#51): present only for a content-predicate read — true if the
        # text/regex matched, false if the wait timed out first.
        if payload.get("matched") is not None:
            out["matched"] = bool(payload.get("matched"))
        # delta (#52): always report the shape so the caller can branch on it;
        # changed_rows is present only for a real delta (the caller then patches
        # its grid model instead of re-reading the whole screen). A full read
        # (or an older agent) reports delta=false.
        out["delta"] = bool(payload.get("delta", False))
        if out["delta"]:
            out["changed_rows"] = payload.get("changed_rows") or []
        if payload.get("degraded"):
            out["degraded"] = True
        return sanic_json(out)

    async def _mcp_input(request: Request):
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        entry, mode, resp = _mcp_entry(request)
        if resp is not None:
            return resp
        if mode != "readwrite":
            return sanic_json({"error": "read_only"}, status=403)
        body = _json_object_body(request) or {}
        data = body.get("data")
        if not isinstance(data, str):
            return sanic_json({"error": "bad_data"}, status=400)
        if len(data.encode("utf-8", "replace")) > MAX_MCP_INPUT_BYTES:
            return sanic_json({"error": "too_large"}, status=413)
        # Deliberately bypasses the single-active-browser lease: MCP is its own
        # authorized channel (gated by the MCP token + readwrite mode), not a
        # browser, so the one-active-browser rule does not apply to it.
        await entry.send_to_producer(protocol.input_frame(data))
        # #33: pulse the robot icon on the write (warm/sharp flash) — only after
        # a validated readwrite send, so a rejected/read-only attempt doesn't
        # flash. Detached task (see _flash_mcp_activity).
        _flash_mcp_activity(entry, "write")
        return sanic_json({"ok": True})

    async def _mcp_reset(request: Request):
        # #27: clear the producer's screen-render buffer (its PTY-output ring)
        # so the next read_screen renders from a clean slate. A mutating
        # terminal-management action (it discards observable history for every
        # viewer), so it needs readwrite — like /mcp/input. Same correlated
        # round-trip as /mcp/read: only an agent answers, so a non-agent
        # producer times out -> 502 no_producer_rpc.
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        entry, mode, resp = _mcp_entry(request)
        if resp is not None:
            return resp
        if mode != "readwrite":
            return sanic_json({"error": "read_only"}, status=403)
        payload, error = await _session_rpc(
            entry, lambda req: protocol.reset_please_frame(req), "reset_done")
        if error is not None:
            return sanic_json({"error": "no_producer_rpc"}, status=502)
        payload = payload or {}
        if not payload.get("ok"):
            return sanic_json({"error": "reset_failed",
                               "detail": payload.get("error")}, status=502)
        return sanic_json({"ok": True, "id": entry.id})

    async def _mcp_profiles(request: Request):
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        return sanic_json({
            "default": app.ctx.launcher.default_profile,
            "profiles": sorted(app.ctx.launcher.profiles.keys()),
        })

    async def _mcp_launch(request: Request):
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        if not app.ctx.mcp_cfg["allow_launch"]:
            return sanic_json({"error": "launch_disabled"}, status=403)
        params, perr = _parse_launch_body(request)
        if perr is not None:
            return perr
        try:
            status, payload = await app.ctx.launcher.launch(
                params["profile"], cols=params["cols"], rows=params["rows"],
                title=params["title"], cwd=params["cwd"])
        except LaunchError as exc:
            return sanic_json(exc.payload, status=exc.status)
        return sanic_json(payload, status=status)

    # ---- MCP config (browser-facing, auth_token-gated) -------------------
    # The Control Panel reads/writes the MCP token + knobs here. Gated by the
    # BROWSER auth_token (loopback-or-token, same as /file/* and /state) — NOT
    # the MCP token — so the secret only ever travels to an already-
    # authenticated browser and never rides the synced /state blob.
    def _mcp_token_env_pinned() -> bool:
        # The admin env override (resolve_token semantics: "env wins so a unit
        # file can override"). When set, the UI must not be able to change the
        # live token — it would drift from what the env pins on next restart.
        return bool(os.environ.get(auth.MCP_TOKEN_ENV))

    def _mcp_cfg_public(cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "enabled": bool(cfg["enabled"]),
                "token": cfg["token"] or "",
                "default_mode": cfg["default_mode"],
                "allow_launch": bool(cfg["allow_launch"]),
                "token_env_pinned": _mcp_token_env_pinned()}

    async def _mcp_config_get(request: Request):
        err = _gated_auth_error(request, "/mcp/config")
        if err is not None:
            return err
        return sanic_json(_mcp_cfg_public(app.ctx.mcp_cfg))

    async def _mcp_config_post(request: Request):
        err = _gated_auth_error(request, "/mcp/config")
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        # Validate everything BEFORE the lock/write so a bad field changes
        # nothing. Build the new cfg from a copy; only the atomic write +
        # ctx swap make it live, so an early return can never half-apply.
        if "default_mode" in body and body.get("default_mode") not in MCP_MODES:
            return sanic_json({"ok": False, "error": "bad_mode"}, status=400)
        if ("token" in body and body.get("token") is not None
                and not isinstance(body.get("token"), str)):
            return sanic_json({"ok": False, "error": "bad_token"}, status=400)
        env_pinned = _mcp_token_env_pinned()
        async with app.ctx.mcp_lock:
            cfg = dict(app.ctx.mcp_cfg)
            # Token edits are honored only when env is NOT pinning it (else the
            # live token would diverge from the env value restart restores).
            if not env_pinned:
                if body.get("generate"):
                    # Server-minted token (never a client-chosen secret here).
                    cfg["token"] = secrets.token_urlsafe(32)
                elif "token" in body:
                    tok = (body.get("token") or "").strip()
                    cfg["token"] = tok or None
            if "default_mode" in body:
                cfg["default_mode"] = body["default_mode"]
            if "allow_launch" in body:
                cfg["allow_launch"] = bool(body.get("allow_launch"))
            if "enabled" in body:
                cfg["enabled"] = bool(body.get("enabled"))
            # Persist an EXPLICIT schema (never the whole dict) so a future
            # internal field can't accidentally land in the sidecar. When env
            # pins the token, write None: _load_mcp_cfg ignores the sidecar
            # token under an env pin anyway, and this keeps the env secret off
            # disk.
            to_persist = {
                "token": None if env_pinned else cfg["token"],
                "default_mode": cfg["default_mode"],
                "allow_launch": bool(cfg["allow_launch"]),
                "enabled": bool(cfg["enabled"]),
            }
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, app.ctx.mcp_state_path,
                    to_persist)
            except OSError as exc:
                return sanic_json({"ok": False, "error": str(exc)}, status=500)
            app.ctx.mcp_cfg = cfg
        return sanic_json(_mcp_cfg_public(cfg))

    # ---- launch profiles config (browser-facing, #70) --------------------
    # The Control Panel reads/writes the FULL profile objects here. Gated by the
    # BROWSER token-or-loopback realm (_gated_auth_error), EXACTLY like /file/*,
    # /state and /mcp/config — never the MCP-token realm. So the commands (the
    # RCE-by-design half of the profiles-only model) only ever travel to an
    # already-authenticated browser; /profiles and /mcp/profiles stay names-only,
    # so an MCP/AI agent still can't read commands or define profiles. A same-
    # machine tokenless page can drive this, the accepted /file/* posture — this
    # write is no weaker than /file/write (both grant persistent host code-exec),
    # so a tokenless broker must not run while the browser visits untrusted sites.
    def _profiles_public_view() -> Dict[str, Any]:
        launcher = app.ctx.launcher
        profiles = launcher.profiles          # live dict; iterate, never mutate
        out: Dict[str, Any] = {}
        exists: Dict[str, bool] = {}
        for name, entry in profiles.items():
            cmd = list(entry.get("command") or [])
            out[name] = {"command": cmd, "title": entry.get("title"),
                         "cwd": entry.get("cwd")}
            # Validate-executable-exists: does command[0] resolve on PATH now?
            # A False marks a profile whose shell isn't installed (UI red flag).
            exists[name] = bool(cmd) and shutil.which(cmd[0]) is not None
        return {"ok": True, "default_profile": launcher.default_profile,
                "profiles": out,
                "os": "windows" if os.name == "nt" else "posix",
                "source": app.ctx.profiles_source, "exists": exists}

    async def _profiles_config_get(request: Request):
        err = _gated_auth_error(request, "/profiles/config")
        if err is not None:
            return err
        return sanic_json(_profiles_public_view())

    async def _profiles_config_post(request: Request):
        err = _gated_auth_error(request, "/profiles/config")
        if err is not None:
            return err
        if request.body and len(request.body) > MAX_PROFILES_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=413)
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        # Validate the WHOLE set BEFORE the lock/write (mirrors _mcp_config_post):
        # a bad field returns 400 and changes nothing.
        result, verr = _validate_profiles_post(body)
        if verr is not None:
            return sanic_json({"ok": False, "error": verr}, status=400)
        to_persist = {"profiles": result["profiles"],
                      "default_profile": result["default_profile"]}
        async with app.ctx.profiles_lock:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, app.ctx.profiles_path,
                    to_persist)
            except OSError as exc:
                return sanic_json({"ok": False, "error": str(exc)}, status=500)
            # Disk is truth first; ONLY on a successful write do we live-swap the
            # launcher, so a failed write never leaves runtime disagreeing with
            # the sidecar. set_profiles rebinds fresh objects (atomic vs launch).
            app.ctx.launcher.set_profiles(result["profiles"],
                                          result["default_profile"])
            app.ctx.profiles_source = "sidecar"
        # Audit: this write persists shell recipes /launch will spawn by name.
        LOGGER.info("launch profiles updated via /profiles/config: %d "
                    "profiles (default=%r) from %s", len(result["profiles"]),
                    result["default_profile"], request.ip)
        return sanic_json(_profiles_public_view())

    async def _profiles_detect(request: Request):
        # Read-only environment scan seeding the editor (WSL distros on Windows,
        # allow-listed shells on POSIX). Browser realm, same gate as the editor.
        # The scan blocks (subprocess/FS), so run it off the event loop.
        err = _gated_auth_error(request, "/profiles/detect")
        if err is not None:
            return err
        suggestions = await asyncio.get_running_loop().run_in_executor(
            None, _detect_profile_suggestions)
        return sanic_json({"ok": True, "suggestions": suggestions})

    # ---- broker identity (/info) -----------------------------------------
    # Non-secret stable id + build version (#64). Gated by the SAME
    # token-or-loopback policy as /state: the same-origin local probe passes via
    # loopback (no token); the cross-origin add-time probe passes via the
    # ?token= appendHostToken already attaches. Gating (vs fully public) keeps a
    # durable broker fingerprint off the unauthenticated network — adding a
    # remote already requires a token anyway.
    async def _info(request: Request):
        err = _gated_auth_error(request, "/info")
        if err is not None:
            return err
        return sanic_json({"ok": True, "broker_id": app.ctx.broker_id,
                           "version": app.ctx.version,
                           "mods_enabled": app.ctx.mods_enabled,
                           "serve_ui": app.ctx.serve_ui})

    # ---- shared UI state (/state) ----------------------------------------
    # Per-broker settings + layout, shared across a user's browsers. Optimistic
    # concurrency on an integer rev: GET returns {rev, settings, layout}; PUT
    # supplies {baseRev, settings, layout} and is rejected 409 (with the
    # current state inlined, so the loser resyncs in one round trip) when
    # baseRev != the live rev. Same token-or-loopback gate as /file/*.
    async def _state_get(request: Request):
        err = _gated_auth_error(request, "/state")
        if err is not None:
            return err
        s = app.ctx.state
        return sanic_json({"rev": s["rev"], "settings": s["settings"],
                           "layout": s["layout"]})

    async def _state_put(request: Request):
        err = _gated_auth_error(request, "/state")
        if err is not None:
            return err
        if request.body and len(request.body) > MAX_STATE_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=413)
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        base_rev = body.get("baseRev")
        settings = body.get("settings")
        layout = body.get("layout")
        client_id = str(body.get("clientId") or "").strip()
        if not isinstance(base_rev, int) or base_rev < 0:
            return sanic_json({"ok": False, "error": "bad_baseRev"},
                              status=400)
        if not isinstance(settings, dict) or not isinstance(layout, dict):
            return sanic_json({"ok": False, "error": "bad_state"}, status=400)
        # Lock the whole lease-check / read-rev / compare / write / bump: the
        # write awaits, so two concurrent PUTs could otherwise interleave on
        # rev. The lease check lives INSIDE the lock so a become_active that
        # linearized while this PUT was queued on the lock is seen — checking
        # before the (awaiting) lock acquire would let a just-deactivated
        # client's in-flight write still clobber the active layout.
        async with app.ctx.state_lock:
            # Single-active-client lease: a non-active browser must not mutate
            # the shared layout/settings (a torn-down/background tab could
            # otherwise clobber the active one). 409 not_active inlines the live
            # state so the loser resyncs in one round trip. A None lease (broker
            # just restarted, nobody has claimed yet) does NOT block — and GET
            # /state stays ungated so a reactivating tab can always read.
            active = app.ctx.active_client_id
            if active is not None and client_id != active:
                s = app.ctx.state
                return sanic_json({
                    "ok": False, "error": "not_active",
                    "rev": s["rev"], "settings": s["settings"],
                    "layout": s["layout"],
                }, status=409)
            current = app.ctx.state
            if base_rev != current["rev"]:
                # Conflict — inline the live state so the client rebases without
                # a second GET (Codex review fix: avoids a 409 retry storm).
                return sanic_json({
                    "ok": False, "error": "conflict",
                    "rev": current["rev"], "settings": current["settings"],
                    "layout": current["layout"],
                }, status=409)
            new_state = {"rev": current["rev"] + 1,
                         "settings": settings, "layout": layout}
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, app.ctx.state_path, new_state)
            except OSError as exc:
                return sanic_json({"ok": False, "error": str(exc)}, status=500)
            app.ctx.state = new_state
        return sanic_json({"ok": True, "rev": new_state["rev"]})

    if app.ctx.serve_ui:
        # Import (and thus assemble) the UI constants here, gated on serve_ui, so
        # a headless broker never reads the NN_* fragments or the wiki. Assembly
        # at create_app time preserves UI mode's loud-at-startup failure for a
        # missing/oversized fragment (ui.assemble() is non-protective); deferring
        # it into the handler would let a broken broker boot "healthy" and only
        # 500 on the first GET /. sys.modules caches the assembled values.
        from .ui import INDEX_HTML
        from .help_corpus import HELP_CORPUS
        app.ctx.index_html = INDEX_HTML
        app.ctx.help_corpus = HELP_CORPUS
        app.add_route(_index, "/", methods=["GET"])
        app.add_route(_help_corpus, "/help-corpus.json", methods=["GET"])
    else:
        app.add_route(_index_headless, "/", methods=["GET"])
    app.add_websocket_route(_browser_ws, "/ws")
    app.add_websocket_route(_control_ws, "/control")
    app.add_websocket_route(_producer_ws, "/browserland")
    app.add_route(_sessions, "/sessions", methods=["GET"])
    app.add_route(_profiles, "/profiles", methods=["GET"])
    app.add_route(_launch, "/launch", methods=["POST"])
    app.add_route(_file_list, "/file/list", methods=["POST"])
    app.add_route(_file_read, "/file/read", methods=["POST"])
    app.add_route(_file_read_chunk, "/file/read_chunk", methods=["POST"])
    app.add_route(_file_write, "/file/write", methods=["POST"])
    app.add_route(_file_upload, "/file/upload", methods=["POST"])
    app.add_route(_file_upload_begin, "/file/upload_begin", methods=["POST"])
    app.add_route(_file_upload_chunk, "/file/upload_chunk", methods=["POST"])
    app.add_route(_file_upload_commit, "/file/upload_commit", methods=["POST"])
    app.add_route(_file_upload_abort, "/file/upload_abort", methods=["POST"])
    app.add_route(_file_delete, "/file/delete", methods=["POST"])
    app.add_route(_file_mkdir, "/file/mkdir", methods=["POST"])
    app.add_route(_file_copy, "/file/copy", methods=["POST"])
    app.add_route(_file_move, "/file/move", methods=["POST"])
    app.add_route(_file_zip, "/file/zip", methods=["POST"])
    app.add_route(_file_unzip, "/file/unzip", methods=["POST"])
    app.add_route(_file_stat, "/file/stat", methods=["POST"])
    app.add_route(_file_setattr, "/file/setattr", methods=["POST"])
    app.add_route(_session_procs, "/session/procs", methods=["POST"])
    app.add_route(_session_kill, "/session/kill", methods=["POST"])
    app.add_route(_session_git, "/session/git", methods=["POST"])
    app.add_route(_session_mcp, "/session/mcp", methods=["POST"])
    app.add_route(_info, "/info", methods=["GET"])
    app.add_route(_state_get, "/state", methods=["GET"])
    app.add_route(_state_put, "/state", methods=["PUT"])
    # MCP HTTP interface (external MCP server) + its browser-facing config.
    app.add_route(_mcp_info, "/mcp/info", methods=["GET"])
    app.add_route(_mcp_terminals, "/mcp/terminals", methods=["GET"])
    app.add_route(_mcp_read, "/mcp/read", methods=["POST"])
    app.add_route(_mcp_input, "/mcp/input", methods=["POST"])
    app.add_route(_mcp_reset, "/mcp/reset", methods=["POST"])
    app.add_route(_mcp_profiles, "/mcp/profiles", methods=["GET"])
    app.add_route(_mcp_launch, "/mcp/launch", methods=["POST"])
    app.add_route(_mcp_config_get, "/mcp/config", methods=["GET"])
    app.add_route(_mcp_config_post, "/mcp/config", methods=["POST"])
    # Launch-profile editor (browser realm; #70). Full objects, never the MCP
    # realm — /profiles + /mcp/profiles stay names-only.
    app.add_route(_profiles_config_get, "/profiles/config", methods=["GET"])
    app.add_route(_profiles_config_post, "/profiles/config", methods=["POST"])
    app.add_route(_profiles_detect, "/profiles/detect", methods=["GET"])
    # Explicit preflights (route resolution precedes request middleware, so
    # an unrouted OPTIONS would 405 before any middleware could answer).
    # Explicit name= per registration — auto-derived names collide.
    for path, route_name in (("/sessions", "preflight_sessions"),
                             ("/profiles", "preflight_profiles"),
                             ("/launch", "preflight_launch"),
                             ("/file/list", "preflight_file_list"),
                             ("/file/read", "preflight_file_read"),
                             ("/file/read_chunk", "preflight_file_read_chunk"),
                             ("/file/write", "preflight_file_write"),
                             ("/file/upload", "preflight_file_upload"),
                             ("/file/upload_begin", "preflight_file_upload_begin"),
                             ("/file/upload_chunk", "preflight_file_upload_chunk"),
                             ("/file/upload_commit", "preflight_file_upload_commit"),
                             ("/file/upload_abort", "preflight_file_upload_abort"),
                             ("/file/delete", "preflight_file_delete"),
                             ("/file/mkdir", "preflight_file_mkdir"),
                             ("/file/copy", "preflight_file_copy"),
                             ("/file/move", "preflight_file_move"),
                             ("/file/zip", "preflight_file_zip"),
                             ("/file/unzip", "preflight_file_unzip"),
                             ("/file/stat", "preflight_file_stat"),
                             ("/file/setattr", "preflight_file_setattr"),
                             ("/session/procs", "preflight_session_procs"),
                             ("/session/kill", "preflight_session_kill"),
                             ("/session/git", "preflight_session_git"),
                             ("/session/mcp", "preflight_session_mcp"),
                             ("/info", "preflight_info"),
                             ("/state", "preflight_state"),
                             ("/mcp/info", "preflight_mcp_info"),
                             ("/mcp/terminals", "preflight_mcp_terminals"),
                             ("/mcp/read", "preflight_mcp_read"),
                             ("/mcp/input", "preflight_mcp_input"),
                             ("/mcp/reset", "preflight_mcp_reset"),
                             ("/mcp/profiles", "preflight_mcp_profiles"),
                             ("/mcp/launch", "preflight_mcp_launch"),
                             ("/mcp/config", "preflight_mcp_config"),
                             ("/profiles/config", "preflight_profiles_config"),
                             ("/profiles/detect", "preflight_profiles_detect")):
        app.add_route(_preflight, path, methods=["OPTIONS"], name=route_name)
    app.error_handler.add(NotFound, _handle_404)

    return app
