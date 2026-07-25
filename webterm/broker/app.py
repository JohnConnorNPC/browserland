"""Sanic broker app: windowed desktop page, browser relay WS, producer WS,
/sessions, /profiles, profiles-only /launch.

Default bind is 127.0.0.1:4445 (4444 was an earlier broker's port).

Auth policy lives in auth.py. WS auth is checked IN-HANDLER, post-upgrade,
closing with code 4401: rejecting the upgrade from HTTP middleware surfaces
in the browser as an opaque close code 1006, indistinguishable from a
network failure (a lesson carried over from an earlier broker).

Auth (#142): a token is REQUIRED on every route and every interface, always.
There is no loopback exemption and no opt-out; with nothing configured the
broker mints one into ``webterm_token.json`` beside the state store. The ONLY
unauthenticated responses are ``GET /`` (the token is typed into that page, and
auth is query/header-only with no cookies, so gating the document would 401
every reload, bookmark and new tab forever), ``GET /help-corpus.json``, and the
OPTIONS preflights. ``/mcp/*`` is a separate realm with its own token.

CORS posture: the UI's multi-host mode has the BROWSER fetch /sessions and
dial /ws directly on every configured broker, so the JSON API needs CORS.
ACAO is ``*`` emitted UNCONDITIONALLY on EVERY response (including the 401 an
unauthenticated request now gets everywhere) — auth is token-in-query/header,
never cookies, so ``*`` introduces no ambient-credential risk and needs no
Vary/origin-echo. It must ride on error responses too or a cross-origin login
probe surfaces as a fetch TypeError ("wrong password" indistinguishable from
"host down") and the taskbar's amber auth chip never appears. CORS only ever
governs *browser* reads; the real gate is the token on every route. Preflights
are explicit OPTIONS routes (route resolution happens before request middleware,
so middleware can't answer them) and unauthenticated by design (they carry no
credentials). ``GET /`` is public but must not be embeddable — X-Frame-Options
and frame-ancestors are set below so an attacker page cannot iframe the real UI
and clickjack a browser that already holds a token. AUTO_EXTEND is pinned off:
sanic-ext, when merely installed, silently injects its own CORS middleware plus
an unauthenticated /docs + /openapi.json.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import errno
import functools
import gzip
import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from sanic import Request, Sanic, Websocket
from sanic.exceptions import NotFound
from sanic.response import empty, html, json as sanic_json, raw as sanic_raw

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

# After this many /browserland upgrades refused for a missing token, log the
# one-time "your old terminals cannot reconnect" hint instead of another
# per-connection warning, and drop the rest to DEBUG. Agents reconnect roughly
# every 10s once backed off, so a handful of rejections is the point at which
# this stops being a blip and starts being the stranded-agent symptom (#142).
_PRODUCER_REJECT_HINT_AT = 5

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
# A well-formed hex SHA-256 (#110). upload_commit's optional `expected_sha256`
# must match this exactly: absent -> unverified (copy); malformed -> a clean 400
# (never a silent downgrade to unverified, never a .lower() 500 on a non-string).
_SHA256_HEX_RE = re.compile(r"[0-9a-fA-F]{64}")
# In-flight upload sessions (#108) live in-memory on app.ctx (the broker runs
# single_process, so one dict is authoritative — see __main__). Bound their
# count so idle/abandoned begins can't exhaust the table, and expire stale ones
# (a browser that closed mid-transfer never sends commit/abort) so their temp
# files don't linger.
MAX_UPLOAD_SESSIONS = 32
UPLOAD_SESSION_TTL = 3600.0        # seconds since begin before a sweep drops it

# /file/list is host-wide, so it can be pointed at a pathological directory
# (a build cache, a mail spool, an SMB share with a million entries). Cap the
# number of entries one listing may return so the JSON payload — and the memory
# both brokers and the browser hold — stays bounded. When the cap bites, the
# response carries ``"truncated": true`` so the UI can say so instead of quietly
# showing a partial directory.
MAX_LIST_ENTRIES = 10000

# Clipboard-image paste (#137): pasted images land in a dedicated paste dir
# under generated names and are swept lazily on each upload — expired by age
# first, then trimmed oldest-first to the count cap — so screenshots can't
# accumulate forever.
PASTE_IMAGE_TTL = 6 * 3600.0       # seconds a pasted image stays on disk
PASTE_IMAGE_MAX_FILES = 64         # cap on retained paste-* files

# Terminal session recordings (#140): the recorder mod streams a finished
# recording into a broker-side recordings dir via its own begin/chunk/commit
# trio (server-generated ids, never a client-named path), then lists/fetches/
# deletes them and keeps timestamped notes in a per-recording sidecar. Unlike
# paste images these are DURABLE user data: no TTL sweep ever deletes a
# committed recording — only POST /recording/delete does. Only abandoned
# in-flight .part temps are swept.
_RECORDING_ID_RE = re.compile(r"rec-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
# #151: the client-minted id linking the segments of one rolling recording. It
# is meta only (never a path component), but it is still an identity the library
# groups by, so keep it to a short opaque token.
_RECORDING_SERIES_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
MAX_RECORDING_BYTES = 256 * 2**20   # cap per committed recording file
MAX_RECORDING_SESSIONS = 4          # concurrent in-flight recording saves
RECORDING_SESSION_TTL = 3600.0      # seconds before an abandoned save is swept
MAX_RECORDING_NOTES = 500           # notes per recording
MAX_RECORDING_NOTE_TEXT = 4096      # chars per note
# #159: the two event-file encodings. New recordings land compressed; the bare
# suffix keeps meaning an uncompressed pre-#159 file, which is durable user data
# that nothing ever rewrites. THE SUFFIX IS THE ENCODING — no path is ever
# chosen by sniffing content, so a corrupt .gz reports as a corrupt gzip instead
# of being silently retried as raw JSONL.
REC_SUFFIX = ".blrec"
REC_SUFFIX_GZ = ".blrec.gz"
# Level 6 (zlib's own default), not 9. Measured on a synthetic-but-realistic
# recording (spinner redraws, coloured log lines, full-screen repaints): 6 gives
# 8.1x at ~27 MiB/s per worker, 9 gives 8.2x at ~16 MiB/s. The extra 1% is not
# worth 1.7x the CPU when this runs in the shared executor on every chunk.
REC_GZIP_LEVEL = 6


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

# /mod-store: a generic per-mod server-backed KV with a baked-in revision ring
# (#124), the durable twin of the per-browser ctx.storage (localStorage). The
# scratchpad mod is the first consumer — its note tabs live here so they survive
# a reload / cache clear and read from every browser on this broker. Cap the
# per-mod value like /state so a single hostile PUT can't balloon the store; the
# ring keeps the last N *distinct* values (the no-op dedupe below means an idle
# autosave never grows it). Worst case on disk is ~N * this per mod, acceptable
# for a single-user loopback tool whose mods are code-reviewed (a mod that could
# fill this could already write anywhere via ctx.file — same trust tier).
MAX_MODSTORE_BYTES = 1 * 2**20      # 1 MiB per-mod value
MODSTORE_MAX_REVISIONS = 50         # revision-ring depth (per mod)
# A mod id is the path segment in /mod-store/<modId>; keep it to the same shape a
# mod dir uses (lowercase, digits, hyphen) so it can never traverse or collide
# with a JSON metadata key. Compiled once; used by the loader + both handlers.
_MODSTORE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

# /session/* management RPCs: how long the broker waits for the producer's
# reply before giving up with 504 (the agent does psutil/git work off its
# event loop, so this is generous).
RPC_TIMEOUT = 10.0

# ---- AI-provider status proxy (#112) ---------------------------------------
# The broker makes NO other outbound HTTP request; GET /status/fetch is its ONLY
# egress. The aistatus mod (which ships DISABLED — no request until the operator
# opts in) needs a server-side, ALLOWLISTED, cached proxy so browser JS can read
# cross-origin Atlassian Statuspage summaries (the status hosts don't send
# permissive CORS, so the browser can't fetch them directly).
#
# SSRF defense is STRUCTURAL, not a filter: the client supplies only an allowlist
# ID (never a URL — see _status_fetch), each ID maps to a FIXED https base here,
# _fetch_status_blocking refuses redirects and dials https only, so neither a
# caller nor a compromised/redirecting status host can pivot the fetch to an
# internal address. v1 ships the four providers confirmed to run Atlassian
# Statuspage; mistral (Instatus) and xai (Cloudflare-gated) are non-Statuspage
# and deferred to the same follow-up as Google/Gemini.
STATUS_ALLOWLIST = {
    "anthropic": {"label": "Anthropic",
                  "base": "https://status.claude.com"},
    "openai":    {"label": "OpenAI",
                  "base": "https://status.openai.com"},
    "cohere":    {"label": "Cohere",
                  "base": "https://status.cohere.com"},
    "copilot":   {"label": "GitHub Copilot",
                  "base": "https://www.githubstatus.com"},
}
STATUS_FETCH_TIMEOUT = 4.0          # seconds per upstream request
STATUS_MAX_BYTES = 512 * 1024       # reject an oversized status payload
STATUS_CACHE_TTL = 60               # seconds a normalized result is cache-served
# Statuspage summary.json overall indicators, worst last. Anything else (or a
# fetch/parse failure) normalizes to "unknown" so the UI greys the row.
_STATUS_INDICATORS = ("none", "minor", "major", "critical")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a compromised/redirecting status host must NOT be
    able to bounce the broker's fetch to an internal address (SSRF). Raising
    here surfaces as an HTTPError the caller catches -> graceful "unknown"."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, "redirect refused (status proxy)", headers, fp)


# Opener built once at import: OUR no-redirect handler (a HTTPRedirectHandler
# subclass, so build_opener drops the permissive default) + an EMPTY ProxyHandler
# so a broker-process HTTP(S)_PROXY/ALL_PROXY env can NOT re-route this egress —
# the only outbound destinations stay the fixed allowlist hosts, dialed direct
# (build_opener would otherwise install the env-honoring default ProxyHandler).
# No redirect following, no proxy, https only (asserted per-fetch).
_STATUS_OPENER = urllib.request.build_opener(
    _NoRedirectHandler, urllib.request.ProxyHandler({}))


def _normalize_statuspage(pid: str, label: str, payload: Any) -> Dict[str, Any]:
    """Reduce a raw Atlassian Statuspage summary.json to the small, fixed shape
    the client renders. Every third-party string is coerced with str() and the
    lists are capped, so a hostile/huge upstream payload can't bloat the response
    or smuggle unexpected types. Unknown indicator -> "unknown"."""
    if not isinstance(payload, dict):
        payload = {}
    status = payload.get("status")
    status = status if isinstance(status, dict) else {}
    indicator = status.get("indicator")
    if indicator not in _STATUS_INDICATORS:
        indicator = "unknown"
    description = str(status.get("description") or "")
    incidents: List[Dict[str, str]] = []
    for inc in (payload.get("incidents") or []):
        if not isinstance(inc, dict):
            continue
        # summary.json lists only UNRESOLVED incidents, but guard anyway.
        if str(inc.get("status") or "") in ("resolved", "postmortem"):
            continue
        incidents.append({"name": str(inc.get("name") or ""),
                          "impact": str(inc.get("impact") or "")})
        if len(incidents) >= 10:
            break
    components: List[Dict[str, str]] = []
    for comp in (payload.get("components") or []):
        if not isinstance(comp, dict):
            continue
        st = str(comp.get("status") or "")
        # Non-operational leaf components only; skip a group CONTAINER (it just
        # aggregates its children's status and would double-report).
        if st and st != "operational" and comp.get("group") is not True:
            components.append({"name": str(comp.get("name") or ""), "status": st})
            if len(components) >= 20:
                break
    return {"id": pid, "label": label, "indicator": indicator,
            "description": description, "incidents": incidents,
            "components": components}


def _fetch_status_blocking(pid: str) -> Dict[str, Any]:
    """Blocking fetch+normalize for ONE provider, run in an executor (stdlib
    urllib, no new dep). Allowlist-backed id -> fixed https base -> summary.json,
    https-only, no redirects, 200-only, size-capped. ANY failure degrades to an
    "unknown" row (never blocks the UI); the exception TYPE name is echoed for a
    quiet client-side hint but nothing upstream is trusted into the response."""
    entry = STATUS_ALLOWLIST[pid]        # KeyError impossible: caller validated
    label = entry["label"]
    url = entry["base"].rstrip("/") + "/api/v2/summary.json"
    try:
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ValueError("non_https_base")   # belt-and-suspenders vs allowlist
        req = urllib.request.Request(url, headers={
            "User-Agent": "browserland-status/1.0 (+#112)",
            "Accept": "application/json",
        })
        with _STATUS_OPENER.open(req, timeout=STATUS_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                raise ValueError("http_%s" % resp.status)
            body = resp.read(STATUS_MAX_BYTES + 1)
        if len(body) > STATUS_MAX_BYTES:
            raise ValueError("too_large")
        payload = json.loads(body.decode("utf-8", "replace"))
        return _normalize_statuspage(pid, label, payload)
    except Exception as exc:   # noqa: BLE001 — any failure is a graceful "unknown"
        LOGGER.info("status fetch failed for %s: %s", pid, type(exc).__name__)
        return {"id": pid, "label": label, "indicator": "unknown",
                "description": "", "incidents": [], "components": [],
                "error": type(exc).__name__}

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

# /mcp/pace (#133): cap a terminal's DEFAULT inter-key pacing (ms). Same ceiling
# as the MCP server's per-call delay_ms cap (_MAX_KEY_DELAY_MS) so a per-terminal
# default can never pace slower than an explicit delay could.
MAX_MCP_PACE_MS = 1000

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
# Optional per-profile DEFAULT terminal color (#115): a strict ``#rrggbb`` hex
# (the first hex validator on the Python side — host color #103 was browser-only).
# Matched with ``.fullmatch`` so a trailing newline can't sneak in past ``$``.
_PROFILE_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}$")


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
    # #115: optional per-profile default color. Self-heal like cwd — keep it only
    # if it is a clean #rrggbb, else drop it (a hand-edited/reloaded sidecar can
    # never carry junk into the seed map or the UI); the profile itself survives.
    color = value.get("color")
    entry["color"] = (color if isinstance(color, str)
                      and _PROFILE_COLOR_RE.fullmatch(color) else None)
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
        # #115: optional per-profile default color. Absent/'' -> None (no
        # default). REPLACE semantics: reject a non-#rrggbb value rather than
        # coerce it, so a bad field changes nothing.
        color = value.get("color")
        if color is not None and color != "":
            if not isinstance(color, str) or not _PROFILE_COLOR_RE.fullmatch(color):
                return None, "bad_color"
        entry["color"] = color or None
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


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: temp file in the SAME directory,
    fd closed, then ``os.replace``. A reader never observes a half-written file
    (visibility only — crash durability is deliberately not promised), and the
    fd is closed before the replace because Windows cannot replace an open file.

    The temp is unlinked on ANY failure — the ``finally`` covers a non-OSError
    too — so a botched write never litters the tree. The one implementation
    behind /file/write, /file/upload and /file/paste_image, which used to carry
    three verbatim copies of this dance.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".webterm-",
                                   suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, str(path))
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_capped(path: Path, cap: int) -> bytes:
    """Read at most ``cap + 1`` bytes from ``path``. One byte PAST the cap so a
    file exactly at the cap still reads while anything larger is detectable by
    length alone, and reading bounded bytes (rather than stat-then-read) closes
    the grow-after-check window and bounds what we ever pull into memory.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    with path.open("rb") as fh:
        return fh.read(cap + 1)


async def _off_loop(fn, *args, timeout=None):
    """Run the BLOCKING callable ``fn(*args)`` on the default executor and await
    its result. The one canonical way to get filesystem / subprocess work off the
    event loop in this module.

    Why it matters: the broker runs ``single_process=True`` — ONE event loop for
    every HTTP handler, every producer WebSocket and every browser relay. A
    blocking call in a handler therefore freezes the WHOLE broker, live terminals
    included, for its full duration. The file API is deliberately host-wide and
    accepts UNC paths, so a single ``/file/list`` against a dead SMB share would
    otherwise wedge everything for the SMB timeout.

    Only INERT data may cross into the worker (paths, bytes, plain values). The
    loop-affine state — ``state_lock`` and the other asyncio locks, the registry
    waiters, the launcher's pending map, ``bg_tasks``, and every Sanic
    ``Request``/response object — is NOT thread-safe: mutate it on the loop and
    hand the worker only what it needs.

    ``timeout`` exists in the signature so the later deadline work can land here
    without touching every call site, but it is NOT implemented and passing one
    RAISES rather than being silently ignored — a deadline argument that quietly
    does nothing is worse than no argument at all.

    Why there is no deadline yet: ``asyncio.wait_for`` around
    ``run_in_executor`` does not kill the worker, because a *running*
    ``concurrent.futures`` future is not cancellable. Wrapping a deadline here
    would only convert "request hangs, loop stays free" into "request 504s, thread
    still wedged" — and enough wedged UNC probes would starve the SHARED default
    executor that ``/status/fetch``, ``/profiles/detect`` and every
    ``_write_state_atomic`` also depend on. Real deadlines need a dedicated,
    bounded probe pool, which is a separate decision and out of scope here."""
    if timeout is not None:
        raise NotImplementedError(
            "_off_loop takes no deadline yet: wait_for cannot cancel a running "
            "executor future, so a timeout would 504 the request and leave the "
            "worker thread wedged in the shared default executor")
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args))


#: Strong references to in-flight :func:`_shielded_region` tasks. The loop holds
#: only WEAK references to tasks, and a shielded task's other referent is the
#: awaiting handler — which is exactly the thing being cancelled. A done callback
#: discards each entry, so this never grows past what is actually running.
_CRITICAL_TASKS: set = set()


def _log_orphaned_section(task) -> None:
    """Log a shielded critical section that failed AFTER its request was gone.

    Such a failure is otherwise INVISIBLE — worse than the usual "Task exception
    was never retrieved" noise. CPython's ``shield`` calls ``inner.exception()``
    from its own done callback whenever the outer future was cancelled,
    deliberately marking the exception retrieved, so the traceback is consumed
    and dropped; and the client is already gone, so there is no 500 either. An
    unexpected KeyError/ValueError inside a critical section would then leave
    disk and memory disagreeing with no trace anywhere at all."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        LOGGER.error("shielded critical section failed after its request was "
                     "cancelled; disk and memory may disagree", exc_info=exc)


async def _shielded_region(lock: asyncio.Lock, section):
    """Acquire ``lock`` CANCELLABLY, then run ``section()`` to completion even if
    this request is cancelled, releasing the lock inside the shielded task.

    ``section`` is a no-argument coroutine FUNCTION (not a coroutine) holding one
    whole ``guard -> await _off_loop(...) -> accounting -> response`` sequence.
    It must NOT take the lock itself: this owns both acquire and release.

    WHY THE SHIELD IS LOAD-BEARING, not belt-and-braces. Sanic awaits handlers
    inline on the connection task and ``connection_lost`` cancels that task
    unconditionally (so does the RESPONSE_TIMEOUT sweep), so a tab close,
    navigation or dropped link cancels a handler mid-flight. Unshielded, a cancel
    landing on ``await _off_loop(...)`` unwinds the ``async with`` and RELEASES
    THE LOCK immediately — while the worker thread keeps going, because a
    *running* ``concurrent.futures`` future cannot be cancelled (the same fact
    that makes :func:`_off_loop` refuse a deadline). Two things break at once:

      - the post-await accounting never runs, so loop-owned state (``received``,
        ``ctx.state``, the popped session) stops matching what is on disk; and
      - the next request for that lock walks straight into the critical section
        while the first worker is still writing — two threads inside one
        ``open(p, 'ab')`` or one temp+``os.replace``.

    WHY ACQUISITION IS DELIBERATELY OUTSIDE THE SHIELD. Shielding the acquire too
    would make every QUEUED waiter cancellation-immune, which is a denial of
    service rather than a safety property: one slow worker (the default executor
    is SHARED with ``/status/fetch``, ``/profiles/detect`` and every
    ``_write_state_atomic``) stalls the holder, later requests queue on the lock,
    each hits the 60 s RESPONSE_TIMEOUT cancel, and each leaves behind an
    immortal waiter still retaining its decoded body. On the process-global locks
    (state, mod-store, mcp, profiles) that would be route-wide. The rule this
    encodes: CANCEL FREELY UNTIL THE FIRST IRREVERSIBLE STEP, NEVER IN THE MIDDLE
    OF ONE. A cancel while queued is free — nothing has been written — and
    behaves exactly as it did before this helper existed.

    WHAT IT DOES NOT BUY, so nobody reads more into it:

      - It does NOT get the response to the client. The socket is already gone;
        the returned value is discarded and Sanic writes its own error. The
        useful work is the SIDE EFFECTS (finishing the write, running the
        accounting, holding the lock throughout) — never the ``Response``.
      - It therefore does NOT turn "the client was told it failed" into "nothing
        happened". A disconnect or response timeout can now be followed by the
        write landing, so a retry may legitimately meet conflict/no_session for
        an operation the client believes failed. That is the deliberate trade: a
        consistent server over a client-visible non-event.
      - It does NOT make ``before_server_stop`` wait for in-flight work. The
        drains take no lock and are a separate loop task, not a cancellation, so
        they race an in-flight append exactly as they did before.
        ``_CRITICAL_TASKS`` is the seam if that is ever addressed.
      - It does NOT survive DIRECT cancellation of the shielded task (loop
        teardown, a bulk task cancel), which reproduces the original bug.
      - It does NOT make a two-file publish atomic, nor protect against a process
        kill. Durability still rests on temp + ``os.replace``.

    So error paths inside ``section`` must STILL mutate their dicts before their
    own awaits."""
    await lock.acquire()          # cancellable ON PURPOSE — see above

    async def _held():
        try:
            return await section()
        finally:
            lock.release()        # in the SHIELDED task, never the caller's

    task = asyncio.ensure_future(_held())
    _CRITICAL_TASKS.add(task)
    task.add_done_callback(_CRITICAL_TASKS.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Nobody will ever observe this task's outcome now, so make a failure
        # loud instead of letting shield silently eat it.
        task.add_done_callback(_log_orphaned_section)
        raise


def _load_modstore(path: Path) -> Dict[str, Any]:
    """Read+self-heal the /mod-store blob (#124): a dict of
    ``modId -> {rev, value, revisions:[{rev, value, ts}]}`` (newest-first ring).

    Self-healing mirrors _load_state: every field is coerced so a hand-edited or
    truncated file can never break startup. Malformed mod ids / records are
    DROPPED (not repaired) so a bad entry can't shadow a good one; ``rev`` is
    clamped >=0; only well-formed revision entries survive, trimmed to the ring
    depth. ``rev`` is persisted (like /state) so a restart preserves optimistic
    ordering. Returns ``{}`` on any read/parse error (a corrupt file degrades to
    an empty store rather than blocking boot — the same accepted degraded state
    /state has)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    store: Dict[str, Any] = {}
    for mod_id, rec in data.items():
        if not isinstance(mod_id, str) or not _MODSTORE_ID_RE.fullmatch(mod_id):
            continue
        if not isinstance(rec, dict):
            continue
        rev = rec.get("rev")
        rev = rev if isinstance(rev, int) and not isinstance(rev, bool) \
            and rev >= 0 else 0
        revisions = []
        raw = rec.get("revisions")
        if isinstance(raw, list):
            for ent in raw:
                if not isinstance(ent, dict):
                    continue
                erev = ent.get("rev")
                ets = ent.get("ts")
                if not isinstance(erev, int) or isinstance(erev, bool) \
                        or erev < 0:
                    continue
                if "value" not in ent:
                    continue
                revisions.append({
                    "rev": erev,
                    "value": ent["value"],
                    "ts": ets if isinstance(ets, int)
                    and not isinstance(ets, bool) else 0,
                })
        # Newest-first, trimmed. Order by rev (the durable ordering); ts is a
        # display-only stamp and is never trusted for ordering/trimming.
        revisions.sort(key=lambda e: e["rev"], reverse=True)
        del revisions[MODSTORE_MAX_REVISIONS:]
        store[mod_id] = {
            "rev": rev,
            "value": rec.get("value") if "value" in rec else None,
            "revisions": revisions,
        }
    return store


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


# Origins allowed to execute script (#143). 'self' covers the vendored xterm
# under /vendor/ -- it used to come from cdn.jsdelivr.net, which is now gone
# from the page entirely. esm.sh is the one remaining third party: it serves
# CodeMirror as ES modules via dynamic import(), which CSP governs under
# script-src (NOT connect-src), and its stubs re-import via ROOT-RELATIVE paths
# so the whole module graph stays on that single origin. It cannot be SRI-
# pinned (its URLs are range-resolved on purpose) and so remains trusted.
_SCRIPT_ORIGINS = ("'self'", "https://esm.sh")


def _csp_header(inline_hash: Optional[str] = None) -> str:
    """The Content-Security-Policy value (#143).

    Deliberately NARROW. Only ``script-src`` (who may execute code) and
    ``frame-ancestors`` (who may embed us) are set: adding ``default-src``,
    ``style-src``, ``img-src`` or ``connect-src`` would have to enumerate every
    inline style, the data: favicon, blob: download URLs and every host a
    federated UI talks to -- each one a way to break the app for no gain against
    the threat this addresses.

    What it buys: a script from an origin NOT listed here cannot execute, even
    if something injects a tag. What it explicitly does NOT buy: protection
    from a compromised package on an origin that IS listed -- SRI is what
    neutralizes jsdelivr, and nothing here saves esm.sh (see #143).

    ``inline_hash`` authorizes our own bundle; without it (headless, which
    serves no page and no inline script) the directive simply lists the
    origins."""
    script_src = " ".join(
        ([f"'{inline_hash}'"] if inline_hash else []) + list(_SCRIPT_ORIGINS))
    return f"script-src {script_src}; frame-ancestors 'none'"


def _open_url(config: Optional[Dict[str, Any]], port: int, token: str) -> str:
    """The ready-to-open desktop URL, token included. A wildcard bind has no
    single address, so show loopback — the operator substitutes their own."""
    host = ((config or {}).get("host") or "127.0.0.1").strip()
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"                              # bare IPv6 literal
    return f"http://{host}:{port}/?token={urllib.parse.quote(token, safe='')}"


def _log_auth_banner(app: Sanic, port: int, config: Optional[Dict[str, Any]],
                     *, minted_into_used_dir: bool) -> None:
    """Say what the token situation is, exactly once, at startup (#142).

    The full ``?token=`` URL is a live credential, so it is printed only when it
    has to be:

    * on the run that MINTED it, and then only to an interactive terminal — a
      long-lived deploy under systemd/Docker/CI would otherwise bake the token
      into a centralized log forever, and there ``--print-token`` recovers it;
    * always when the token is EPHEMERAL, because nothing persisted it and
      ``--print-token`` genuinely cannot get it back.

    Every other run logs a pointer, never the value."""
    token = app.ctx.auth_token
    source = app.ctx.auth_token_source
    path = app.ctx.auth_state_path
    try:
        interactive = bool(sys.stderr and sys.stderr.isatty())
    except (AttributeError, ValueError):
        interactive = False

    if source == "ephemeral":
        LOGGER.warning(
            "AUTH TOKEN IS EPHEMERAL - could not write %s. A new token is "
            "generated on EVERY restart, '--print-token' cannot recover it, "
            "and terminals launched by this run will not survive a restart. "
            "Make the directory writable, or set auth_token in broker_config "
            "/ $%s. Open: %s",
            path, auth.TOKEN_ENV, _open_url(config, port, token))
        return

    if source == "minted":
        if minted_into_used_dir:
            # This broker has run here before, without a token — i.e. an
            # upgrade, not a fresh install. Say what just broke.
            LOGGER.warning(
                "=== UPGRADE NOTICE (#142): a token is now REQUIRED on every "
                "connection, including loopback. This broker previously ran "
                "with no token, so: (1) browsers will ask for one - the login "
                "overlay appears on first load; (2) TERMINALS LAUNCHED BY THE "
                "PREVIOUS RUN CANNOT RECONNECT and must be relaunched (their "
                "shells keep running, but they were started without the token "
                "and it cannot be injected into a live process); (3) scripts "
                "calling /sessions, /launch or /file/* over loopback now get "
                "401. A token has been minted into %s - recover it any time "
                "with 'python -m webterm.broker --print-token'. See "
                "docs/UPGRADING.md. ===", path)
        else:
            LOGGER.info("token auth enabled (minted a new token into %s)", path)
        if interactive:
            LOGGER.info("open the desktop at: %s", _open_url(config, port, token))
        else:
            LOGGER.info("run 'python -m webterm.broker --print-token' for the "
                        "token and the URL to open")
        return

    where = {"env": f"${auth.TOKEN_ENV}",
             "config": "broker_config auth_token"}.get(source, str(path))
    LOGGER.info("token auth enabled (source=%s, %s) - "
                "'python -m webterm.broker --print-token' prints it", source,
                where)


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
    follows (it is non-destructive to the source).

    NOTE: the ``/file/*`` handlers now use :func:`_probe_two` instead, which does
    this resolution plus the follow-up stats in ONE off-loop hop. Prefer that —
    calling this from a handler puts two blocking resolves back on the loop."""
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


class _Probe(NamedTuple):
    """Everything the ``/file/*`` prologues need to know about ONE path, gathered
    in a single worker hop. A field the caller did NOT ask for is an inert
    default (``""`` / ``None``) — never a lie about the filesystem, just "not
    asked", and (importantly) not a syscall that was never run today either.

      ``path``         the resolved path (per ``follow_leaf``)
      ``kind``         ``_classify_path(path)``: file|dir|other|missing|denied,
                       or ``""`` when ``want_kind`` is False
      ``parent_kind``  ``_classify_path(path.parent)``, or ``""`` unless
                       ``want_parent``
      ``lexists``      ``os.path.lexists(path)`` — TRUE for a broken symlink,
                       which ``kind`` reports as 'missing'
      ``is_link``      ``_is_reparse_point(path)``: symlink OR Windows junction
      ``target``       the SAME rel resolved with ``follow_leaf=True``, or
                       ``None`` unless ``want_target``
      ``target_kind``  ``_classify_path(target)`` — see the short-circuit note on
                       ``_probe_path_sync``; ``""`` unless ``want_target``

    ``lexists`` and ``is_link`` are the only two computed unconditionally: both
    are pure ``lstat``, so they never traverse a link and can never block on a
    link's (possibly dead, possibly UNC) target."""
    path: Path
    kind: str
    parent_kind: str
    lexists: bool
    is_link: bool
    target: Optional[Path]
    target_kind: str


def _probe_path_sync(rel: str, default_dir: Path, follow_leaf: bool = True,
                     want_kind: bool = True, want_parent: bool = False,
                     want_target: bool = False) -> _Probe:
    """Blocking body of :func:`_probe_path` — resolve + stat one path. Runs in a
    worker thread; raises ``ValueError`` (-> ``bad_path``) exactly where
    ``_resolve_host_path`` does.

    The ``want_*`` flags are NOT an optimisation, they are a correctness
    requirement: a classify FOLLOWS the leaf, so probing eagerly for a handler
    that never classified that path today would add a brand-new traversal — and
    for a symlink/junction pointing at a dead SMB share that is a brand-new hang
    where today's code short-circuited first. Ask only for what the handler
    actually inspects.

    ``target_kind`` carries the same rule as an internal short-circuit: it is
    computed only when the link-safe leaf EXISTS and is NOT a link, mirroring
    ``/file/delete``, which returns not_found or removes the link entry before it
    ever classifies through to a target."""
    p = _resolve_host_path(rel, default_dir, follow_leaf=follow_leaf)
    p_str = str(p)
    # lstat only — never traverses, so it is safe to run unconditionally.
    lexists = os.path.lexists(p_str)
    is_link = _is_reparse_point(p_str)
    target = None
    target_kind = ""
    if want_target:
        target = _resolve_host_path(rel, default_dir, follow_leaf=True)
        if lexists and not is_link:
            target_kind = _classify_path(target)
    return _Probe(
        path=p,
        kind=_classify_path(p) if want_kind else "",
        parent_kind=_classify_path(p.parent) if want_parent else "",
        lexists=lexists,
        is_link=is_link,
        target=target,
        target_kind=target_kind,
    )


async def _probe_path(rel: str, default_dir: Path, follow_leaf: bool = True,
                      want_kind: bool = True, want_parent: bool = False,
                      want_target: bool = False) -> _Probe:
    """Resolve and stat ONE client-supplied path in a single off-loop hop.

    This is the batched replacement for the ``_resolve_host_path`` +
    ``_classify_path`` (+ ``_is_reparse_point`` + ``lexists``) sequence the
    ``/file/*`` handlers used to run one call at a time ON the event loop. Every
    one of those is a blocking syscall against a host-wide, possibly-UNC path;
    together they were the broker's single biggest source of loop stalls.

    Raises ``ValueError`` on a resolver failure so callers keep returning the
    same ``{"ok": false, "error": "bad_path"}`` 400 as before.

    IMPORTANT — the caller must still branch in TODAY'S ORDER. The probe gathers
    what was asked for eagerly, but WHICH error a bad request produces is decided
    purely by the order the handler inspects these fields, so that order is
    load-bearing and must not be "tidied".

    TOCTOU: batching NARROWS the window BETWEEN THE CHECKS — they used to be
    separate blocking syscalls with the whole cost of each one between them, and
    are now adjacent inside one worker. It does add an await boundary before the
    handler's body, so another coroutine on this loop can interleave there where
    a fully-blocking prologue would have kept it out; that is accepted, because
    the file API is check-then-act by construction and its host-wide, single-user
    threat model already assumes the caller's own shells are mutating the same
    tree concurrently. Do NOT "fix" this back to sequential on-loop calls: it
    would trade a theoretical narrowing for freezing every live terminal on the
    broker for the duration of a UNC stat."""
    return await _off_loop(_probe_path_sync, rel, default_dir, follow_leaf,
                           want_kind, want_parent, want_target)


def _probe_two_sync(src_rel: str, dst_rel: str, default_dir: Path,
                    src_follow_leaf: bool, dst_follow_leaf: bool,
                    want_src_kind: bool, want_dst_kind: bool,
                    want_src_parent: bool,
                    want_dst_parent: bool) -> Tuple[_Probe, _Probe]:
    """Blocking body of :func:`_probe_two`."""
    return (
        _probe_path_sync(src_rel, default_dir, follow_leaf=src_follow_leaf,
                         want_kind=want_src_kind, want_parent=want_src_parent),
        _probe_path_sync(dst_rel, default_dir, follow_leaf=dst_follow_leaf,
                         want_kind=want_dst_kind, want_parent=want_dst_parent),
    )


async def _probe_two(body: Dict[str, Any], default_dir: Path,
                     src_key: str = "src", dst_key: str = "dst",
                     src_follow_leaf: bool = True,
                     dst_follow_leaf: bool = True,
                     want_src_kind: bool = True, want_dst_kind: bool = True,
                     want_src_parent: bool = False,
                     want_dst_parent: bool = False) -> Tuple[_Probe, _Probe]:
    """Two-path sibling of :func:`_probe_path` for the copy/move/zip/unzip
    handlers: read the two path fields out of ``body``, resolve and stat BOTH in
    ONE off-loop hop, and return ``(src_probe, dst_probe)``.

    The batched replacement for ``_resolve_two`` + the four-to-nine follow-up
    ``_classify_path`` / ``lexists`` calls those handlers ran on the loop —
    ``/file/copy`` alone drops from two resolves plus nine stats to one hop.

    ``src_key``/``dst_key`` name the body fields because the callers disagree:
    copy/move use ``src``/``dst``, zip uses ``src``/``dest``, unzip uses
    ``path``/``dest``. A missing, non-string or empty field raises ``ValueError``
    (-> ``bad_path``), the same outcome those handlers produced from their own
    per-field validation, and in the same src-then-dst order.

    ``*_follow_leaf`` picks link-safe leaf resolution per side, exactly as
    ``_resolve_two`` did: move resolves both link-safe (it relocates a link
    ENTRY, never the target tree); copy follows (non-destructive to the source).

    ``want_*_kind`` default True but MUST be turned off for a side the handler
    never classified before — ``/file/move`` is exactly that case (it decides on
    ``lexists`` alone, precisely so a symlink src is relocated without its target
    ever being stat'ed). See the flag note on :func:`_probe_path_sync`.

    The ordering and TOCTOU notes on :func:`_probe_path` apply verbatim."""
    src_rel = body.get(src_key)
    dst_rel = body.get(dst_key)
    if not isinstance(src_rel, str) or not src_rel:
        raise ValueError("bad_path")
    if not isinstance(dst_rel, str) or not dst_rel:
        raise ValueError("bad_path")
    return await _off_loop(_probe_two_sync, src_rel, dst_rel, default_dir,
                           src_follow_leaf, dst_follow_leaf,
                           want_src_kind, want_dst_kind,
                           want_src_parent, want_dst_parent)


def _list_dir(rel: str, default_dir: Path):
    """Blocking body of ``/file/list``: resolve + classify + scan, all in ONE
    worker hop (it used to be a resolve, a classify and then an ``iterdir`` with
    a per-child ``is_dir`` + ``stat``, every one of them on the event loop).

    Returns ``(path, kind, entries, truncated)``. ``entries`` is empty unless
    ``kind == "dir"`` — the caller maps every other kind to its own error, so
    there is nothing to scan.

    ``os.scandir`` (not ``Path.iterdir``) as a CONTEXT MANAGER: the ``DirEntry``
    already carries the stat data, halving the syscalls per child, and closing
    the iterator releases the directory handle — which on Windows is the
    difference between a listable directory and one nobody can rename or delete.

    At most ``MAX_LIST_ENTRIES`` entries are returned; ``truncated`` says the cap
    bit. The sort happens AFTER truncation, so a truncated listing is an
    arbitrary (filesystem-order) subset, sorted — the cap is a payload guard, not
    a paging API.

    Raises ``ValueError`` (-> ``bad_path``) from the resolver and ``OSError``
    from the scan; the caller maps both exactly as before."""
    d = _resolve_host_path(rel, default_dir)
    kind = _classify_path(d)
    entries: List[Dict[str, Any]] = []
    truncated = False
    if kind != "dir":
        return d, kind, entries, truncated
    with os.scandir(str(d)) as it:
        for child in it:
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = True
                break
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue                       # unreadable entry — skip it
            entries.append({"name": child.name,
                            "type": "dir" if is_dir else "file",
                            "size": size})
    # Dirs first, then case-insensitive by name.
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return d, kind, entries, truncated


def _unlink_quiet(paths: List[str]) -> None:
    """Best-effort ``os.unlink`` of every path, swallowing OSError per entry so
    one already-gone (or locked) temp never hides the rest. The single place
    upload/recording session teardown removes temps, so a whole batch rides ONE
    worker hop instead of one blocking syscall per file.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


def _mkstemp_part(parent: Path, prefix: str) -> str:
    """Create — and immediately close — a ``.part`` temp under ``parent``,
    returning its path. The temp MUST live beside the destination so commit's
    ``os.replace`` is an atomic same-filesystem swap, which is why the caller
    passes the dest parent rather than a temp dir.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=prefix, suffix=".part")
    os.close(fd)
    return tmp


def _append_chunk(tmp: str, data: bytes) -> None:
    """Append ONE upload/recording chunk to the session temp.

    BLOCKING: call it through ``_off_loop``, never straight from a handler.

    The caller MUST hold the session's ``asyncio.Lock`` across the whole
    guard -> append -> accounting sequence. Running the append in a worker turns
    what used to be an await-free (and therefore atomic-by-construction) run of
    handler code into one with a yield point in the middle: without the lock two
    concurrent chunk POSTs for the same session would BOTH clear the offset
    guard, BOTH append, and feed the rolling SHA-256 out of order — silently
    corrupting the transfer and the #110 checksum contract.

    CANCELLATION is handled by the CALLER, which runs this whole locked sequence
    through :func:`_shielded_region`. That is load-bearing, not belt-and-braces:
    a client disconnect cancels the handler task, and an unshielded region would
    release the lock while this worker thread kept writing (a running
    ``concurrent.futures`` future cannot be cancelled — the same reason
    ``_off_loop`` takes no deadline). The bytes would land on disk unaccounted,
    and a retry of that offset would append them AGAIN.

    Do not "simplify" that shield away on the theory that the #110 checksum
    catches it. It does not: the commit digest is built from what the handler
    ACCOUNTED for, never from the bytes on disk, so a duplicated append still
    produces a digest matching ``expected_sha256`` and publishes a corrupt file
    behind a 200 — which, for a cross-host move, is what authorises deleting the
    source."""
    with open(tmp, "ab") as fh:
        fh.write(data)


def _append_chunk_gz(tmp: str, data: bytes) -> None:
    """Append ONE recording chunk as its own gzip MEMBER to the session temp.

    BLOCKING: call it through ``_off_loop``, never straight from a handler. The
    caller's lock/shield contract is exactly :func:`_append_chunk`'s and is
    unchanged by compression — that contract guards accounting ORDER, and the
    order does not change here.

    Compressing per chunk rather than gzipping the finished temp at commit is
    what keeps the append STREAMING: ``_recording_commit`` stays a size + an
    ``os.replace``, with no extra full read+write of up to ``MAX_RECORDING_BYTES``
    in a worker while the client waits. Concatenated gzip members are a valid
    gzip stream (RFC 1952 §2.2), and ``gzip.open``/``gzip.decompress``/``zcat``
    read them transparently. Measured cost of the framing at 2 MiB chunks: 0.4%
    worse than one member, i.e. nothing.

    The known cost, recorded rather than waved off: a reader built on a bare
    ``zlib.decompressobj(wbits=31)`` stops at the end of the FIRST member and
    reports success, unless it loops on ``unused_data``. That is not
    hypothetical — httpx's response decoder does it — and it is exactly why
    ``_recording_get`` decodes server-side instead of passing these bytes
    through as ``Content-Encoding: gzip``. Read that comment before changing
    either. (``gzip --list`` has the same shape of wart: it reports the last
    member's sizes rather than the whole file's.)

    ``filename=""`` keeps the session temp's path out of every member header
    (it would otherwise be taken from ``fileobj.name``), and ``mtime=0`` keeps
    members byte-reproducible instead of stamping wall-clock into each one."""
    if not data:
        # An empty chunk is a legal no-op on the raw path; here it would append
        # a 20-byte member carrying nothing.
        return
    with open(tmp, "ab") as fh:
        with gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=REC_GZIP_LEVEL,
                           filename="", mtime=0) as gz:
            gz.write(data)


def _commit_replace(tmp: str, dest: str, overwrite: bool = True) -> int:
    """Size the finished session temp and ``os.replace`` it onto ``dest``,
    returning the size. ONE hop for both syscalls so nothing can grow the temp
    between the measurement and the swap.

    When ``overwrite`` is False the existence check and the swap must be ONE
    indivisible act, which is why it lives here and not in the handler. The
    handler's per-session lock cannot serialize this: two commits for DIFFERENT
    sessions aimed at the same dest hold DIFFERENT locks, so a plain
    ``lexists`` -> ``replace`` pair lets both observe an absent dest and both
    "succeed", silently clobbering the first writer. ``O_CREAT | O_EXCL`` hands
    the decision to the kernel instead: exactly one caller creates the name, and
    the loser gets FileExistsError. The swap then replaces our own placeholder.
    (Before the session IO moved off the loop those two syscalls were await-free
    and therefore atomic by construction — this restores that guarantee.)

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    size = os.path.getsize(tmp)
    if not overwrite:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        try:
            os.replace(tmp, dest)
        except OSError:
            # Never leave the zero-byte reservation behind as a phantom file.
            # We created it, so removing it cannot destroy anyone else's data.
            try:
                os.unlink(dest)
            except OSError:
                pass
            raise
        return size
    os.replace(tmp, dest)
    return size


def _commit_failed_cleanup(tmp: str, dest: str) -> bool:
    """Teardown after a failed commit replace, in ONE hop: unlink the temp, then
    report whether ``dest`` is a directory — the one cause worth naming in the
    error. Same order the three separate on-loop calls used to run in.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    _unlink_quiet([tmp])
    return os.path.isdir(dest)


def _reap_upload_sessions(uploads: Dict[str, Any], now: float) -> List[str]:
    """Pop chunked-upload sessions (#108) older than ``UPLOAD_SESSION_TTL`` and
    RETURN their temp paths, leaving the unlinking to the caller.

    ON THE LOOP, always: ``app.ctx.uploads`` is loop-owned and not thread-safe,
    so the mutation can never move into a worker — only the inert list of paths
    crosses over. That split is the whole reason this is a ``_reap_`` and not the
    old blocking ``_sweep_``.

    Called lazily on each upload_begin so a transfer the browser abandoned
    (closed before commit/abort) can't leak a temp file or permanently hold a
    session slot. Keyed by ``created`` (not last-write) so a genuinely stuck/idle
    session is reclaimed even if it never appended."""
    stale = [uid for uid, s in uploads.items()
             if now - s.get("created", now) > UPLOAD_SESSION_TTL]
    temps = []
    for uid in stale:
        s = uploads.pop(uid, None)
        if s:
            temps.append(s["tmp"])
    return temps


def _sweep_upload_temps(temps: List[str], parent: Path, now: float) -> None:
    """All of upload_begin's cleanup IO in ONE worker hop: unlink the temps the
    loop's :func:`_reap_upload_sessions` just orphaned, then drop crash-orphaned
    ``.part`` files under ``parent``. One hop rather than two because every
    ``await`` is another point where a concurrent begin can interleave.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    _unlink_quiet(temps)
    _sweep_orphan_parts(parent, now)


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


def _reap_rec_sessions(sessions: Dict[str, Dict[str, Any]],
                       now: float) -> List[str]:
    """Pop in-flight recording-save sessions older than RECORDING_SESSION_TTL
    and RETURN their temp paths (#140) — the recording twin of
    :func:`_reap_upload_sessions`, and ON THE LOOP for the same reason
    (``app.ctx.rec_uploads`` is loop-owned). Committed recordings are NEVER
    swept — they are durable user data; only abandoned begins are."""
    temps = []
    for rec_id in list(sessions.keys()):
        session = sessions.get(rec_id)
        if session is None or now - session["created"] <= RECORDING_SESSION_TTL:
            continue
        sessions.pop(rec_id, None)
        temps.append(session["tmp"])
    return temps


def _sweep_rec_temps(temps: List[str], rec_dir: Path, now: float) -> None:
    """recording_begin's cleanup IO in ONE worker hop, mirroring
    :func:`_sweep_upload_temps`.

    BLOCKING: call it through ``_off_loop``, never straight from a handler."""
    _unlink_quiet(temps)
    _sweep_rec_orphan_parts(rec_dir, now)


def _sweep_rec_orphan_parts(rec_dir: Path, now: float) -> None:
    """Best-effort removal of stale ``.webterm-rec-*.part`` temps under the
    recordings dir (#140) — a crash orphans a save session's temp exactly like
    the upload case (_sweep_orphan_parts). Committed recordings (*.blrec and
    *.blrec.gz) and the meta/notes sidecars are never candidates."""
    try:
        candidates = list(rec_dir.glob(".webterm-rec-*.part"))
    except OSError:
        return
    for child in candidates:
        try:
            if now - child.stat().st_mtime > RECORDING_SESSION_TTL:
                child.unlink()
        except OSError:
            pass


class _RecPaths(NamedTuple):
    """Every file belonging to one VALIDATED recording id (#140, #159).

    ``gz`` and ``raw`` are the two possible event files. New recordings land as
    ``gz``; ``raw`` is a pre-#159 uncompressed recording. Both are built from
    the same id-regex-validated stem plus a FIXED suffix, so the regex remains
    the whole traversal defense for the new suffix exactly as it was for the old
    one.

    Nothing may assume one encoding per recording, and nothing may assume one
    encoding per #151 ``series``: a segment roll can cross a broker restart, so
    two segments of one chain can differ."""
    rec_id: str
    gz: Path
    raw: Path
    meta: Path
    notes: Path

    @property
    def events(self) -> Tuple[Path, Path]:
        """Both event-file candidates, newest encoding FIRST.

        This is the resolution order for every read. Ids are a timestamp plus 4
        random bytes so both files existing for one id essentially cannot happen
        on its own — but a user can copy files into the recordings dir, so the
        tie is broken the same way everywhere (gzip wins in list, get, notes and
        delete) rather than left for each caller to disagree about."""
        return (self.gz, self.raw)


def _rec_sanitize_meta(meta: Any) -> Dict[str, Any]:
    """Whitelist + type-check the client-supplied recording meta (#140). Only
    known scalar fields survive, each clamped/coerced, so the meta sidecar can
    be listed and inlined without trusting the client shape."""
    out: Dict[str, Any] = {}
    if not isinstance(meta, dict):
        return out
    title = meta.get("title")
    if isinstance(title, str) and title:
        out["title"] = title[:300]
    for key in ("cols", "rows", "startedAt", "durationMs",
                "events", "bytes", "fontSize"):
        v = meta.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        # json.loads accepts NaN/Infinity — int() on those raises, so a
        # malicious/buggy payload must never reach the conversion.
        if isinstance(v, float) and not math.isfinite(v):
            continue
        iv = int(v)
        if 0 <= iv <= 2**53:
            out[key] = iv
    font = meta.get("fontFamily")
    if isinstance(font, str) and font:
        out["fontFamily"] = font[:200]
    # #151 segment chain: `series` links the segments a rolling recording was
    # split into, `seg` is the 1-based position in that chain. Unlike `title`,
    # `series` is an IDENTITY — truncating an over-long one would silently fold
    # two distinct chains together, so a malformed id is REJECTED (the segment
    # still saves, just unlinked) rather than clamped. `seg` is only meaningful
    # from 1 up, so a 0 is dropped too.
    series = meta.get("series")
    if isinstance(series, str) and _RECORDING_SERIES_RE.fullmatch(series):
        out["series"] = series
    # Unlike the other numerics, `seg` is an ORDERING key, so a non-integral
    # value is rejected rather than truncated: int(1.9) == 1 would file a second
    # segment as the first one.
    seg = meta.get("seg")
    if not isinstance(seg, bool) and isinstance(seg, (int, float)):
        if isinstance(seg, float) and (not math.isfinite(seg)
                                       or not seg.is_integer()):
            seg = None
        if seg is not None and 1 <= int(seg) <= 2**53:
            out["seg"] = int(seg)
    return out


def _rec_load_json(path: Path) -> Optional[Dict[str, Any]]:
    """A recording sidecar (meta/notes) parsed as a dict, or None when missing
    or corrupt — a broken sidecar degrades to defaults, never a 500."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _rec_load_notes(path: Path) -> Dict[str, Any]:
    """The notes sidecar normalized to {rev:int, notes:[{t,text}]} — missing /
    corrupt / wrong-shaped content degrades to an empty rev-0 record."""
    parsed = _rec_load_json(path)
    if parsed is None:
        return {"rev": 0, "notes": []}
    rev = parsed.get("rev")
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        rev = 0
    notes = []
    raw_notes = parsed.get("notes")
    if isinstance(raw_notes, list):
        for n in raw_notes[:MAX_RECORDING_NOTES]:
            if not isinstance(n, dict):
                continue
            t = n.get("t")
            text = n.get("text")
            if isinstance(t, bool) or not isinstance(t, (int, float)):
                continue
            if isinstance(t, float) and not math.isfinite(t):
                continue
            if not isinstance(text, str):
                continue
            notes.append({"t": int(t), "text": text})
    return {"rev": rev, "notes": notes}


def _sniff_image_kind(data: bytes) -> Optional[str]:
    """Magic-byte sniff for the clipboard-image formats browsers hand out
    (#137), used to pick the file extension. A format hint, NOT security
    validation — the file is only ever read back by tools the same user
    points at it (the same trust /file/upload already extends to arbitrary
    bytes at arbitrary paths)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _sweep_paste_images(paste_dir: Path, now: float) -> None:
    """Best-effort retention sweep of ``paste-*`` files (#137): unlink those
    older than PASTE_IMAGE_TTL, then trim oldest-first so that after the
    caller writes its new file at most PASTE_IMAGE_MAX_FILES remain. Runs
    synchronously in the (single-process) upload handler BEFORE the new file
    is written, so the just-uploaded image is never the one trimmed. Any
    error is swallowed — a file we can't stat or unlink simply isn't swept."""
    try:
        fresh = []
        for child in paste_dir.glob("paste-*"):
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if now - mtime > PASTE_IMAGE_TTL:
                try:
                    child.unlink()
                except OSError:
                    pass
            else:
                fresh.append((mtime, child))
    except OSError:
        return
    fresh.sort()                       # oldest first
    excess = len(fresh) - (PASTE_IMAGE_MAX_FILES - 1)
    for _, child in fresh[:max(0, excess)]:
        try:
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


async def _vendor_asset(request: Request, name: str):
    """Vendored xterm (#143). Public like "/" — the browser fetches these to
    render the login page itself, before any token exists, and a <script src>
    cannot carry an Authorization header. Static bytes from the wheel: nothing
    host- or session-derived. Served from an in-memory allowlist dict, so a
    client-supplied name can only ever hit a known key (no traversal)."""
    asset = request.app.ctx.vendor.get(name)
    if asset is None:
        return sanic_json({"ok": False, "error": "not_found"}, status=404)
    body, ctype = asset
    # Immutable: the filename is version-pinned by what we vendored, and the
    # bytes only change in a commit that also changes the wheel.
    return sanic_raw(body, content_type=ctype,
                     headers={"Cache-Control": "public, max-age=31536000, immutable"})


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
    # The token rides in the query string (auth.py: never log request URLs), and
    # Sanic's access log writes the full path+query for every request. Pin it off
    # so a live credential can't be tailed out of broker.log.
    app.config.ACCESS_LOG = False
    app.ctx.config = config
    # app.ctx.auth_token is resolved (and minted if need be) AFTER the state path
    # below — the token sidecar is its sibling — and before the Launcher captures
    # it. Nothing between here and there reads it.
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
    # Clipboard-image paste dir (#137): where /file/paste_image lands blobs
    # under generated paste-* names. Config "paste_dir" overrides; the default
    # is a dedicated subdir of the host temp dir so the retention sweep only
    # ever touches our own files. Created lazily per upload (an OS temp
    # cleaner may remove it between uploads), 0o700 on POSIX.
    app.ctx.paste_dir = Path(
        config.get("paste_dir")
        or (Path(tempfile.gettempdir()) / "webterm-paste"))
    # AI-provider status cache (#112): provider id -> {"at": loop.time(),
    # "data": normalized}. Populated lazily by GET /status/fetch and re-fetched
    # after STATUS_CACHE_TTL. Single-process => this one dict is the source of
    # truth; a minor concurrent-miss stampede is acceptable for v1.
    app.ctx.status_cache = {}
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
    # ---- browser auth token (#142) ---------------------------------------
    # MANDATORY on every surface and every interface — there is no loopback
    # exemption and no opt-out. Resolved here, after state_path (the sidecar is
    # its sibling; override with "auth_state_path") and before the Launcher
    # captures it for the agents it spawns. Never None.
    #
    # "Has this state dir been used before?" is sampled BEFORE anything below
    # creates a sidecar of its own (_load_or_create_broker_id writes the
    # identity file), because a mint on a dir that already holds broker state
    # means an install that used to run tokenless — the UPGRADE NOTICE case.
    app.ctx.auth_state_path = Path(
        config.get("auth_state_path")
        or (app.ctx.state_path.parent / auth.AUTH_STATE_FILENAME)
    ).resolve()
    _dir_was_used = (app.ctx.state_path.exists()
                     or (app.ctx.state_path.parent
                         / "webterm_identity.json").exists())
    app.ctx.auth_token, app.ctx.auth_token_source = auth.resolve_or_mint_token(
        config, app.ctx.auth_state_path)
    # Count of /browserland upgrades refused for a missing token, so the warning
    # can rate-limit itself and, at the threshold, explain the symptom once
    # (agents from a previous tokenless broker retry forever). Old-code agents
    # hammer regardless of the fix on our side.
    app.ctx.producer_rejects = 0
    # Baseline CSP. Replaced below with the hash-bearing variant when serve_ui
    # is on; headless serves no inline script, so the origin list is enough.
    app.ctx.csp = _csp_header()
    # Terminal session recordings (#140): durable user data, so the default
    # lives BESIDE the state store (not the temp dir an OS cleaner may wipe).
    # Config "recordings_dir" overrides. Created lazily on first save.
    # app.ctx.rec_uploads holds the in-flight begin/chunk/commit save sessions,
    # keyed by server-generated recording id (same in-memory single_process
    # posture as app.ctx.uploads); its lock serializes the notes read-rev /
    # compare / write / bump like the state/modstore locks.
    app.ctx.recordings_dir = Path(
        config.get("recordings_dir")
        or (app.ctx.state_path.parent / "webterm_recordings")).resolve()
    app.ctx.rec_uploads = {}
    app.ctx.rec_notes_lock = asyncio.Lock()
    # Generic per-mod server store for /mod-store (#124) — the durable, cross-
    # browser twin of ctx.storage. Persisted in its own sidecar beside /state
    # (override with "modstore_path"); rev lives in the file so a restart
    # preserves optimistic ordering (same reasoning as /state). Its own lock
    # serializes the read-rev / compare / write / bump on PUT (the file write
    # awaits, so two PUTs could otherwise interleave on rev). Reuses the /state
    # single-active-client lease: a non-active browser READS but can't write.
    app.ctx.modstore_path = Path(
        config.get("modstore_path")
        or (app.ctx.state_path.parent / "webterm_modstore.json")
    ).resolve()
    app.ctx.modstore = _load_modstore(app.ctx.modstore_path)
    app.ctx.modstore_lock = asyncio.Lock()
    LOGGER.info("mod store: %s (%d mod%s)", app.ctx.modstore_path,
                len(app.ctx.modstore),
                "" if len(app.ctx.modstore) == 1 else "s")
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
    _log_auth_banner(app, port, config, minted_into_used_dir=_dir_was_used)

    async def _cors_headers(request: Request, response):
        # Unconditional ACAO:* (see module docstring) — token-gating it left a
        # tokenless network-reachable broker unable to answer the UI's
        # cross-origin /sessions fetch. Sanic runs response middleware on error
        # paths too (401/404/405), which the cross-origin login probe depends
        # on — pinned by tests.
        response.headers["Access-Control-Allow-Origin"] = "*"
        # The token rides in the query string, so the desktop URL IS a
        # credential. Without this, any outbound link the user follows from the
        # UI (or from recorded terminal output) hands the full ?token= URL to a
        # third party in the Referer header.
        response.headers["Referrer-Policy"] = "no-referrer"
        # GET / is deliberately public so the login overlay can bootstrap — but
        # public must not mean embeddable. An attacker page that iframes the
        # real UI cannot READ across origins, yet it can still clickjack a
        # browser that already holds a token into launching a shell, pasting
        # into a terminal or writing a file. X-Frame-Options for older browsers,
        # frame-ancestors for the rest.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = app.ctx.csp
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

    async def _preflight(request: Request, **_params):
        # 204; the CORS response middleware decorates it. **_params absorbs any
        # route path parameter (e.g. /mod-store/<modId>) Sanic passes as a kwarg
        # so this one shared handler serves parametric preflights too.
        return empty()

    async def _browser_ws(request: Request, ws: Websocket):
        # In-handler post-upgrade auth: see module docstring (4401 vs 1006).
        if not auth.request_token_ok(request, app.ctx.auth_token):
            LOGGER.warning("rejected unauthenticated /ws from %s", request.ip)
            await ws.close(code=4401, reason="auth required")
            return
        await relay.handle_browser_ws(request, ws, app.ctx.registry, app.ctx)

    async def _control_ws(request: Request, ws: Websocket):
        # Per-browser control channel for the single-active-browser lease.
        # Same post-upgrade auth gate as /ws (4401, never a 1006-opaque upgrade
        # refusal). An unauthenticated /control could otherwise steal the
        # single-active-browser lease out from under the real client.
        if not auth.request_token_ok(request, app.ctx.auth_token):
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
        # Producers need the token too (#142). Loopback used to be exempt here
        # even WITH a token configured — the one gate the token never covered —
        # and WebSockets are not CORS-gated, so any web page could dial
        # ws://127.0.0.1:<port>/browserland, re-register a live window_id
        # (kicking the real agent off with 1012) and inject fabricated terminal
        # output. Agents get the token from the Launcher via $WEB_TERMINAL_TOKEN
        # and append it to this dial themselves.
        if not auth.request_token_ok(request, app.ctx.auth_token):
            app.ctx.producer_rejects += 1
            rejects = app.ctx.producer_rejects
            if rejects == _PRODUCER_REJECT_HINT_AT:
                # Rate-limits the warning AND explains the symptom: an agent
                # spawned by a previous tokenless broker retries forever and its
                # window never comes back. Nothing server-side can rescue it —
                # an env var cannot be injected into a live process.
                LOGGER.warning(
                    "%d producer connections rejected for a missing token - "
                    "terminals launched by a previous tokenless broker cannot "
                    "reconnect and must be relaunched (see docs/UPGRADING.md). "
                    "Further rejections are logged at DEBUG.",
                    rejects)
            elif rejects < _PRODUCER_REJECT_HINT_AT:
                LOGGER.warning(
                    "rejected unauthenticated /browserland from %s", request.ip)
            else:
                LOGGER.debug(
                    "rejected unauthenticated /browserland from %s", request.ip)
            await ws.close(code=4401, reason="auth required")
            return
        await run_producer_session(ws, app.ctx.registry)

    async def _sessions(request: Request):
        # 401 (never 403): the login overlay and the taskbar host chips pop on
        # 401 only, and /sessions is the probe both of them use.
        if not auth.request_token_ok(request, app.ctx.auth_token):
            return sanic_json({"ok": False, "error": "auth_required"},
                              status=401)
        # Stamp each summary's effective MCP mode so the UI's window menu can
        # tick the right radio off the existing 2s poll (no extra fetch).
        return sanic_json(app.ctx.registry.session_summaries(
            app.ctx.mcp_cfg["default_mode"]))

    async def _profiles(request: Request):
        # Same gate as /sessions. Names only — command/cwd never leave the
        # broker.
        if not auth.request_token_ok(request, app.ctx.auth_token):
            return sanic_json({"ok": False, "error": "auth_required"},
                              status=401)
        profs = app.ctx.launcher.profiles
        return sanic_json({
            "default": app.ctx.launcher.default_profile,
            "profiles": sorted(profs.keys()),
            # #115: additive name -> #rrggbb side-map of the optional per-profile
            # DEFAULT colors (only profiles that set one). The "profiles" array
            # stays names-only, so the names-only invariant holds and no
            # command/cwd ever rides this; the seed path reads it to color a new
            # terminal by its launch profile.
            "colors": {n: e["color"] for n, e in profs.items()
                       if isinstance(e, dict) and e.get("color")},
            # OS of this broker's host so the UI can pick the matching per-OS
            # default start path (issue #2). "windows" | "posix" — never a
            # path or anything host-identifying.
            "os": "windows" if os.name == "nt" else "posix",
        })

    async def _parse_launch_body(request: Request):
        """Parse + validate a launch request body, shared by /launch and
        /mcp/launch. Returns ``(params, None)`` where params is the kwargs for
        ``launcher.launch``, or ``(None, error_response)``.

        ``cwd`` is the only client-supplied parameter that is more than dims/
        title — but it is DATA (the shell's cwd), never a command. It is
        validated as an existing directory and normalized to an absolute,
        symlink-collapsed path (rejecting the validate/spawn drift Codex
        flagged). Empty/missing -> the agent's default cwd. Not confined to a
        root: this is the broker's own host and a single-user token/loopback
        tool (a launch_root config could tighten it if ever bound wider).

        Async because that validation is filesystem work on a client-supplied
        path — realpath+isdir against a UNC path pointing at a dead share would
        otherwise freeze the whole broker (one loop, every terminal on it) for
        the SMB timeout. Both syscalls ride ONE ``_off_loop`` hop, which narrows
        the realpath/isdir window between them; it does add an await boundary
        before ``launcher.launch``, so the directory can be removed between the
        check and the spawn — accepted, since that race exists regardless (the
        agent is what actually opens the cwd, milliseconds later) and the spawn
        failure is already reported as ``spawn_failed``. Do not "fix" this back
        into blocking on-loop calls."""
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
                raw_cwd = cwd

                def _resolve_cwd():
                    """Blocking half: resolve then classify, in one hop.
                    Returns (resolved_or_None, is_dir)."""
                    try:
                        resolved = os.path.realpath(raw_cwd)
                    except (OSError, ValueError):
                        return None, False
                    return resolved, os.path.isdir(resolved)

                cwd, cwd_is_dir = await _off_loop(_resolve_cwd)
                if cwd is None:
                    return None, sanic_json({"ok": False, "error": "bad_cwd"},
                                            status=400)
                if not cwd_is_dir:
                    return None, sanic_json({"ok": False,
                                             "error": "cwd_not_dir"},
                                            status=400)
            else:
                cwd = None
        return {"profile": body.get("profile"), "cols": cols, "rows": rows,
                "title": title, "cwd": cwd}, None

    async def _launch(request: Request):
        err = _gated_auth_error(request, "/launch")
        if err is not None:
            return err
        params, err = await _parse_launch_body(request)
        if err is not None:
            return err
        try:
            status, payload = await app.ctx.launcher.launch(
                params["profile"], cols=params["cols"], rows=params["rows"],
                title=params["title"], cwd=params["cwd"])
        except LaunchError as exc:
            return sanic_json(exc.payload, status=exc.status)
        return sanic_json(payload, status=status)

    # ---- token gate (shared by /file/*, /state and ~40 other routes) ------
    # A token is REQUIRED, always, on every interface (#142). There is no
    # loopback exemption: `tailscale serve` in front of a 127.0.0.1 bind makes
    # every tailnet request arrive from loopback, and a page in the same browser
    # reaches loopback too — which for /file/* meant host-wide read/write.
    # ALWAYS 401 `auth_required`, never 403: the login overlay and the taskbar
    # host chips pop on 401 only (63_js_clipboard_auth.js, 75_js_taskbar_hosts.js),
    # and the mods match on that exact error string. CORS headers ride on every
    # response via _cors_headers, including this one, or a cross-origin login
    # probe surfaces as a fetch TypeError instead of "wrong password".
    def _gated_auth_error(request: Request, label: str):
        if not auth.request_token_ok(request, app.ctx.auth_token):
            LOGGER.warning("rejected unauthenticated %s from %s",
                           label, request.ip)
            return sanic_json({"ok": False, "error": "auth_required"},
                              status=401)
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
        # ONE off-loop hop for the resolve + classify + scan (was three-plus
        # blocking rounds on the event loop, one syscall per directory entry —
        # against a host-wide path that may be a dead UNC share). The classify
        # branches below still run in exactly the order they always did, so the
        # same bad request still produces the same error.
        try:
            d, kind, entries, truncated = await _off_loop(
                _list_dir, str(body.get("path") or ""), app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "dir":
            return sanic_json({"ok": False, "error": "not_a_directory"},
                              status=400)
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
            # True when MAX_LIST_ENTRIES bit: `entries` is a partial listing.
            "truncated": truncated,
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
            probe = await _probe_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        try:
            # Capped at MAX_FILE_BYTES + 1 so oversize is detectable by length
            # alone; off the loop because this file may live on a dead share.
            raw = await _off_loop(_read_capped, p, MAX_FILE_BYTES)
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
            probe = await _probe_path(rel, app.ctx.editor_root,
                                      want_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind not in ("file", "missing"):
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        # Leaf first, then parent — this handler's order, unchanged.
        pkind = probe.parent_kind
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        # Atomic mkstemp -> write -> os.replace, off the loop (see
        # _write_bytes_atomic for the semantics and the temp cleanup).
        try:
            await _off_loop(_write_bytes_atomic, p, data)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
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
            probe = await _probe_path(rel, app.ctx.editor_root,
                                      want_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind in ("dir", "other"):
            return sanic_json({"ok": False, "error": "not_a_file"},
                              status=400)
        if kind == "file" and not overwrite:
            return sanic_json({"ok": False, "error": "exists"},
                              status=409)
        # Leaf first, then parent — this handler's order, unchanged.
        pkind = probe.parent_kind
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        try:
            await _off_loop(_write_bytes_atomic, p, data)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p),       # absolute, host-wide (#35)
                           "size": len(data)})

    async def _file_paste_image(request: Request):
        # Clipboard-image paste (#137): the UI uploads the browser-side image
        # blob here and pastes the returned path into the terminal — the only
        # way an image can cross from the browser's clipboard (possibly on
        # another machine) into a PTY app on this host, whose own S4U window
        # station has no clipboard worth reading. Unlike /file/upload the
        # caller names no path: blobs land ONLY in paste_dir under a generated
        # name and are TTL+count swept, so this is strictly narrower than the
        # write-anywhere /file/upload living under the same auth gate.
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        b64 = body.get("content_b64")
        if not isinstance(b64, str) or not b64:
            return sanic_json({"ok": False, "error": "bad_content"},
                              status=400)
        # Cheap pre-decode cap (codex): an oversized payload is rejectable
        # from the base64 length alone (4/3 expansion) before buying the
        # decode; the decoded-size check below stays authoritative.
        if len(b64) > (MAX_FILE_BYTES * 4) // 3 + 8:
            return sanic_json({"ok": False, "error": "too_large"}, status=400)
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            return sanic_json({"ok": False, "error": "bad_base64"},
                              status=400)
        if len(data) > MAX_FILE_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=400)
        kind = _sniff_image_kind(data)
        if kind is None:
            return sanic_json({"ok": False, "error": "not_an_image"},
                              status=400)
        paste_dir = app.ctx.paste_dir
        # mkdir(mode, parents, exist_ok) positionally — 0o700 on POSIX, the
        # default 0o777 (ignored by Windows) elsewhere, exactly as before.
        mode = 0o700 if os.name == "posix" else 0o777
        try:
            await _off_loop(paste_dir.mkdir, mode, True, True)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        # Retention sweep still runs BEFORE the new file is written (so the
        # just-pasted image is never the one trimmed), just not on the loop.
        await _off_loop(_sweep_paste_images, paste_dir, time.time())
        name = "paste-%s-%s.%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                   secrets.token_hex(4), kind)
        p = paste_dir / name
        try:
            await _off_loop(_write_bytes_atomic, p, data)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p),
                           "size": len(data),
                           "kind": kind})

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
        # One hop for BOTH resolutions (link-safe leaf + followed target), the
        # lexists and reparse-point probes, and the target classify. want_kind is
        # OFF: this handler never classified the link-safe leaf, and doing so
        # would stat THROUGH a link — the traversal the link branch below exists
        # to avoid. target_kind carries the same short-circuit (see
        # _probe_path_sync), so the branch order and outcomes are unchanged.
        try:
            probe = await _probe_path(rel, app.ctx.editor_root,
                                      follow_leaf=False, want_kind=False,
                                      want_target=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, p_leaf = probe.target, probe.path
        leaf_str = str(p_leaf)
        # lexists, not exists: a broken symlink (target gone) still has a link
        # entry that should be deletable, and a real link must be detected here
        # before any classification follows it.
        if not probe.lexists:
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if probe.is_link:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _remove_link, leaf_str)
            except (OSError, ValueError, shutil.Error, RecursionError) as exc:
                return sanic_json({"ok": False, "error": str(exc)}, status=400)
            return sanic_json({"ok": True, "path": leaf_str})
        kind = probe.target_kind
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
    # context menu. Same token gate, host-wide resolution
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
            probe = await _probe_path(rel, app.ctx.editor_root,
                                      want_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind != "missing":
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        # Leaf first, then parent — this handler's order, unchanged.
        pkind = probe.parent_kind
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
        # One hop for both resolves + all three classifies (src, dst, dst.parent).
        # The src -> dst -> dst-parent branch order below is TODAY's order and is
        # what decides which error a bad request gets — do not reorder it.
        try:
            src_p, dst_p = await _probe_two(body, app.ctx.editor_root,
                                            want_dst_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        src, dst = src_p.path, dst_p.path
        if src == dst:
            return sanic_json({"ok": False, "error": "same"}, status=400)
        if _is_within(dst, src):
            return sanic_json({"ok": False, "error": "dest_in_source"},
                              status=400)
        src_kind = src_p.kind
        if src_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if src_kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if src_kind not in ("file", "dir"):
            return sanic_json({"ok": False, "error": "not_supported"},
                              status=400)
        dst_kind = dst_p.kind
        if dst_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        dparent = dst_p.parent_kind
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
            # Rollback lives HERE, inside the worker thread, rather than in the
            # handler's except clause: rmtree of a half-copied tree is recursive
            # blocking IO, and running it after the failure had propagated back
            # to the loop froze the whole broker — every live terminal socket
            # included — for the length of the delete. The guards are unchanged:
            # only a !overwrite dst (freshly created by this op, so ours to
            # remove) is ever touched, and a reparse point is unlinked, never
            # recursed into. An overwrite wrote over a caller-owned dst in place
            # and is still reported, not rolled back.
            try:
                if src_kind == "dir":
                    shutil.copytree(src_str, dst_str, symlinks=True,
                                    dirs_exist_ok=overwrite)
                else:
                    shutil.copy2(src_str, dst_str)
            except (OSError, ValueError, shutil.Error, RecursionError):
                if not overwrite:
                    # Remove the partial litter. Anything that goes wrong in
                    # here is swallowed so the bare `raise` below still carries
                    # the ORIGINAL copy failure (and its traceback) to the
                    # client — a broken rollback must never mask the real error.
                    # Broad on purpose: rmtree(ignore_errors=True) eats OSError,
                    # so what could still escape is the non-OSError tail (a
                    # RecursionError off a very deep partial tree, say), and
                    # that would now BE the executor's exception rather than a
                    # 500 out of the handler.
                    try:
                        if (os.path.isdir(dst_str)
                                and not _is_reparse_point(dst_str)):
                            shutil.rmtree(dst_str, ignore_errors=True)
                        elif os.path.lexists(dst_str):
                            os.unlink(dst_str)
                    except Exception:
                        pass
                raise

        try:
            await asyncio.get_running_loop().run_in_executor(None, _do_copy)
        except (OSError, ValueError, shutil.Error, RecursionError) as exc:
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
        # One hop for both link-safe resolves, both lexists probes and the
        # dst-parent classify; the branch order is unchanged. NEITHER side is
        # classified (want_*_kind off) because this handler never classified
        # them: move decides on lexists alone, exactly so a symlink/junction is
        # relocated as an ENTRY with no stat through to its target.
        try:
            src_p, dst_p = await _probe_two(body, app.ctx.editor_root,
                                            src_follow_leaf=False,
                                            dst_follow_leaf=False,
                                            want_src_kind=False,
                                            want_dst_kind=False,
                                            want_dst_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        src, dst = src_p.path, dst_p.path
        if src == dst:
            return sanic_json({"ok": False, "error": "same"}, status=400)
        if _is_within(dst, src):
            return sanic_json({"ok": False, "error": "dest_in_source"},
                              status=400)
        src_str, dst_str = str(src), str(dst)
        # lexists (not exists): a broken symlink leaf still exists and must be
        # movable; a real symlink/junction must not be dereferenced here.
        if not src_p.lexists:
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        dparent = dst_p.parent_kind
        if dparent == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dparent != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        dst_exists = dst_p.lexists
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
        # _probe_two does the same src-then-dest field validation (missing /
        # non-string / empty -> bad_path) and then both resolves plus all three
        # classifies in ONE hop. Branch order below is unchanged.
        try:
            src_p, dest_p = await _probe_two(body, app.ctx.editor_root,
                                             src_key="src", dst_key="dest",
                                             want_dst_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        src, dest = src_p.path, dest_p.path
        src_kind = src_p.kind
        if src_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if src_kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if src_kind not in ("file", "dir"):
            return sanic_json({"ok": False, "error": "not_supported"},
                              status=400)
        dest_kind = dest_p.kind
        if dest_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dest_kind == "dir":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)
        if dest_kind != "missing" and not overwrite:
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        dparent = dest_p.parent_kind
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
        # Same one-hop probe as /file/zip, with this handler's field names
        # (path/dest); the path-then-dest validation and the branch order below
        # are unchanged.
        try:
            z_p, dest_p = await _probe_two(body, app.ctx.editor_root,
                                           src_key="path", dst_key="dest",
                                           want_dst_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        zpath, dest = z_p.path, dest_p.path
        zkind = z_p.kind
        if zkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if zkind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if zkind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)
        dest_kind = dest_p.kind
        if dest_kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if dest_kind != "missing":
            return sanic_json({"ok": False, "error": "exists"}, status=409)
        dparent = dest_p.parent_kind
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
            probe = await _probe_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)

        def _stat_path():
            # The tail's stat AND the shallow child scan in ONE worker hop, for
            # the same reason the probe above runs off the loop: both are
            # blocking syscalls against a host-wide, possibly-UNC path, and on
            # this single-loop broker either one would freeze every live
            # terminal for its full duration. Same shape as the already-
            # offloaded _hash_file below. Branch order is preserved: a failing
            # stat still short-circuits before the scan, and an unreadable dir
            # still swallows its OSError and simply reports no count.
            st = p.stat()
            children = None
            if kind == "dir":
                try:
                    children = sum(1 for _ in p.iterdir())
                except OSError:
                    pass                       # unreadable dir — omit count
            return st, children

        try:
            st, children = await _off_loop(_stat_path)
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
        if children is not None:
            out["children"] = children
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
            probe = await _probe_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
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
            probe = await _probe_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)

        def _read_range():
            # stat per call so eof reflects the CURRENT size (best-effort live
            # read; a file that grows/shrinks mid-stream converges each round —
            # #110 adds a checksum for integrity, out of scope here).
            size = p.stat().st_size
            with p.open("rb") as fh:
                fh.seek(offset)
                return size, fh.read(length)

        try:
            # OFF the loop, like the _hash_file sibling below: this is the
            # download hot path — one stat + one open + one seek + up to
            # MAX_CHUNK_BYTES of read PER CHUNK — so on a big or remote file the
            # on-loop version froze every live terminal once per 4 MiB.
            size, raw = await _off_loop(_read_range)
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

    async def _file_hash(request: Request):
        # #110: stream a file through SHA-256 and return the hex digest. A cross-
        # host MOVE hashes the SOURCE here (a bounded, local streaming re-read on
        # the source broker) so upload_commit can verify the dest matches before
        # the source is deleted. Read in MAX_CHUNK_BYTES blocks OFF the event loop
        # (heavy IO, like copy/zip below): bounded memory, and — like read_chunk —
        # NOT bound by the 5 MiB MAX_FILE_BYTES (a >5 MiB file hashes fine). Same
        # auth gate + host-wide resolution + classify guards as read_chunk.
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
            probe = await _probe_path(rel, app.ctx.editor_root)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p, kind = probe.path, probe.kind
        if kind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if kind == "missing":
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        if kind != "file":
            return sanic_json({"ok": False, "error": "not_a_file"}, status=400)

        def _hash_file():
            # `size` is the bytes actually READ+HASHED (not a separate stat), so
            # the returned size and digest always describe the same byte stream
            # even if the file changes mid-hash (codex review).
            h = hashlib.sha256()
            total = 0
            with p.open("rb") as fh:
                while True:
                    block = fh.read(MAX_CHUNK_BYTES)
                    if not block:
                        break
                    total += len(block)
                    h.update(block)
            return total, h.hexdigest()

        try:
            size, digest = await asyncio.get_running_loop().run_in_executor(
                None, _hash_file)
        except (OSError, ValueError) as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({
            "ok": True,
            "path": str(p),                 # absolute, host-wide (#35)
            "sha256": digest,
            "size": size,
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
            probe = await _probe_path(rel, app.ctx.editor_root,
                                      follow_leaf=False, want_parent=True)
        except ValueError:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        p = probe.path
        parent = p.parent
        # PARENT FIRST, then the leaf — this handler's order, unlike write/upload
        # above, and it is what decides parent_missing vs exists on a bad request.
        pkind = probe.parent_kind
        if pkind == "denied":
            return sanic_json({"ok": False, "error": "permission_denied"},
                              status=400)
        if pkind != "dir":
            return sanic_json({"ok": False, "error": "parent_missing"},
                              status=400)
        kind = probe.kind
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
        #
        # The reap (which is what actually frees a slot) and the cap check stay
        # ADJACENT with no await between them: app.ctx.uploads is loop-owned, so
        # keeping the read-modify-check on one loop turn is what stops two
        # concurrent begins from both slipping past MAX_UPLOAD_SESSIONS. Only the
        # disk half — unlinking the reaped temps + the orphan scan — goes to a
        # worker, and it runs even on the 429 path so a reaped temp is never
        # left behind.
        now = time.time()
        stale = _reap_upload_sessions(app.ctx.uploads, now)
        over_cap = len(app.ctx.uploads) >= MAX_UPLOAD_SESSIONS
        await _off_loop(_sweep_upload_temps, stale, parent, now)
        if over_cap:
            return sanic_json({"ok": False, "error": "too_many_sessions"},
                              status=429)
        try:
            tmp = await _off_loop(_mkstemp_part, parent, ".webterm-up-")
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        # Authoritative cap check, ADJACENT to the insert with no await between
        # the two: the sweep and mkstemp hops above are yield points, so a
        # concurrent begin can have claimed the last slot while we were in them.
        # (The check before the sweep is only the fast path that skips the
        # mkstemp entirely.) Losing here means dropping the temp we just made.
        if len(app.ctx.uploads) >= MAX_UPLOAD_SESSIONS:
            await _off_loop(_unlink_quiet, [tmp])
            return sanic_json({"ok": False, "error": "too_many_sessions"},
                              status=429)
        upload_id = secrets.token_hex(16)
        app.ctx.uploads[upload_id] = {
            "tmp": tmp, "dest": str(p), "overwrite": overwrite,
            "received": 0, "created": now,
            # #110: accumulate the dest digest as chunks are written, so commit
            # can verify it against the source with no re-read. A chunkless
            # (0-byte) session commits sha256(b"") — matches an empty source.
            "hash": hashlib.sha256(),
            # Serializes this session's chunk appends and its commit. Lives IN
            # the session dict on purpose: popping the session drops the only
            # reference, so there is no side table of locks to leak. Created
            # exactly once, here, at begin — never lazily in the chunk path,
            # where two concurrent requests would each mint their own.
            "lock": asyncio.Lock(),
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
        # ---- the atomic region ------------------------------------------
        # guard -> append -> accounting USED to be await-free, which made it
        # atomic by construction: the loop could not interleave two requests
        # inside it. The append now rides a worker, so that yield point is real
        # and the per-session lock is what restores the guarantee. Without it two
        # concurrent POSTs at the same offset would BOTH pass the guard, BOTH
        # append, and feed the rolling digest out of order — a silently corrupt
        # file that still "verifies" against nothing (#110).
        #
        # The lock is loop-affine: taken and released by _shielded_region, on the
        # loop, with only the inert temp path and bytes crossing into the worker.
        # It is held across NOTHING but this sequence.
        #
        # CANCELLATION: a client disconnect cancels this handler task (Sanic's
        # connection_lost). Left unshielded the locked region would unwind and
        # release the lock the moment the await was cancelled, WHILE the worker
        # thread kept writing — a running concurrent.futures future cannot be
        # cancelled. The bytes then land on disk with `received`/`hash` never
        # accounting for them, and because the commit's digest is built from the
        # ACCOUNTING rather than from the file, re-sending that same offset
        # appends the bytes a second time and STILL produces a digest matching
        # expected_sha256 — publishing a corrupt file behind a 200. (For a
        # cross-host move that 200 is what authorises deleting the source.)
        # Shielding runs the whole locked sequence to completion so disk and
        # accounting can never diverge; a disconnected client merely loses the
        # response and its retry hits the offset guard, as it should.
        async def _locked_append():
            # The session can have been popped (commit/abort/too_large) while
            # we waited for the lock, and appending to a popped session's temp
            # would resurrect a file nobody will ever clean up. Identity, not
            # just presence: only the exact dict we locked counts.
            if app.ctx.uploads.get(upload_id) is not session:
                return sanic_json({"ok": False, "error": "no_session"},
                                  status=404)
            # Ordering guard: the client streams sequentially, so this chunk
            # must start exactly where the last ended. A gap/dup/reorder is
            # rejected WITHOUT appending (never silently corrupts the temp).
            if offset != session["received"]:
                return sanic_json(
                    {"ok": False, "error": "bad_offset",
                     "received": session["received"]}, status=409)
            if session["received"] + len(data) > MAX_TRANSFER_BYTES:
                # Past the per-session ceiling: drop the whole session (temp +
                # slot) so a runaway transfer can't keep consuming disk.
                app.ctx.uploads.pop(upload_id, None)
                await _off_loop(_unlink_quiet, [session["tmp"]])
                return sanic_json({"ok": False, "error": "too_large"},
                                  status=400)
            try:
                await _off_loop(_append_chunk, session["tmp"], data)
            except OSError as exc:
                # A failed append leaves the temp in an unknown state — drop
                # the session so the client can never commit a corrupt file.
                app.ctx.uploads.pop(upload_id, None)
                await _off_loop(_unlink_quiet, [session["tmp"]])
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=400)
            session["received"] += len(data)   # only after a successful write
            session["hash"].update(data)       # #110: hash exactly the
            #   committed bytes — the offset guard above rejected any
            #   dup/reorder before the write, so each byte is fed to the
            #   digest once, in order.
            return sanic_json({"ok": True,
                               "received": session["received"]})

        return await _shielded_region(session["lock"], _locked_append)

    async def _file_upload_commit(request: Request):
        # Finalize: atomically os.replace the temp onto the dest. Re-checks the
        # exists race unless overwriting, then (#110) verifies the accumulated
        # SHA-256 against an optional expected_sha256 BEFORE the replace. On any
        # failure the temp + session are dropped (nothing leaks).
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
        # Commit takes the SAME per-session lock the chunk path holds. It used to
        # be await-free end to end, so it could not interleave with an append;
        # now BOTH await, and an append landing between the size measurement and
        # the replace would commit bytes the digest never saw. Everything under
        # the lock is bounded (one lexists, one replace, one teardown).
        #
        # Shielded for the same reason the chunk path is (see _shielded_region),
        # with a commit-specific payoff: a cancel inside the _commit_replace hop
        # PUBLISHES the file and then skips the session pop, so the session leaks
        # until UPLOAD_SESSION_TTL — and with overwrite=false a retry now sees its
        # own published dest, returns 409 exists at the lexists pre-check, and
        # doesn't even reclaim it. Shielding makes "published" and "session
        # popped" happen together.
        async def _locked_commit():
            if app.ctx.uploads.get(upload_id) is not session:
                return sanic_json({"ok": False, "error": "no_session"},
                                  status=404)   # a racing commit/abort won
            dest, tmp = session["dest"], session["tmp"]
            # Fast path + error PRECEDENCE: this keeps "exists" ahead of
            # bad_sha256/checksum_mismatch exactly as before. It is NOT the
            # guarantee — it can't be, since a concurrent commit for a
            # different session holds a different lock and would pass it too.
            # The authoritative check is the O_CREAT|O_EXCL reserve inside
            # _commit_replace below, which turns the loser into
            # FileExistsError.
            if not session["overwrite"] and await _off_loop(os.path.lexists,
                                                            dest):
                return sanic_json({"ok": False, "error": "exists"},
                                  status=409)
            # #110: verify the accumulated SHA-256 BEFORE the atomic replace,
            # so a mismatched (truncated/corrupt/source-changed) transfer
            # never overwrites the dest — the existing dest is left intact and
            # only the temp is dropped.
            #   - absent expected_sha256 (copy) -> no comparison, replace as
            #     before.
            #   - present-but-malformed         -> 400 bad_sha256, session
            #     KEPT (a bad request must never silently downgrade a verified
            #     move to unverified, nor 500 on a non-string .lower()).
            #   - present + digest mismatch     -> 409 checksum_mismatch, temp
            #     dropped, session popped, dest NOT replaced.
            expected = body.get("expected_sha256")
            if expected is not None:
                if (not isinstance(expected, str)
                        or not _SHA256_HEX_RE.fullmatch(expected)):
                    return sanic_json({"ok": False, "error": "bad_sha256"},
                                      status=400)
                expected = expected.lower()   # hex is case-insensitive
            digest = session["hash"].hexdigest()   # idempotent — read twice ok
            if expected is not None and digest != expected:
                app.ctx.uploads.pop(upload_id, None)
                await _off_loop(_unlink_quiet, [tmp])
                return sanic_json({"ok": False, "error": "checksum_mismatch",
                                   "sha256": digest},
                                  status=409)   # dest NOT replaced
            try:
                size = await _off_loop(_commit_replace, tmp, dest,
                                       session["overwrite"])
            except FileExistsError:
                # Lost the atomic race for the name: another session committed
                # this dest between our lexists check and the reserve. Same 409
                # the pre-check produces, so the client sees one behaviour. The
                # session is dropped like any other terminal failure.
                app.ctx.uploads.pop(upload_id, None)
                await _off_loop(_unlink_quiet, [tmp])
                return sanic_json({"ok": False, "error": "exists"},
                                  status=409)
            except OSError as exc:
                # replace-over-dir (dest turned into a dir since begin) or any
                # IO error: drop temp + session, report a clear code.
                app.ctx.uploads.pop(upload_id, None)
                dest_is_dir = await _off_loop(_commit_failed_cleanup, tmp,
                                              dest)
                code = "is_dir" if dest_is_dir else str(exc)
                return sanic_json({"ok": False, "error": code}, status=400)
            app.ctx.uploads.pop(upload_id, None)
            return sanic_json({"ok": True, "path": dest, "size": size,
                               "sha256": digest})   # +sha256 (additive)

        return await _shielded_region(session["lock"], _locked_commit)

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
        # The pop is the whole mutation and it happens on the loop, so abort stays
        # atomic. It deliberately does NOT take the session lock: abort is the
        # client's escape hatch and must never queue behind an append that is
        # wedged on a dead share. Two accepted costs: an append racing this abort
        # can recreate the temp it just removed (bounded and self-healing —
        # _sweep_orphan_parts reaps stray .webterm-up-*.part files by age), and an
        # abort issued while a commit is already inside the lock loses, so the
        # upload lands anyway. Both need a client that aborts and writes the same
        # session at once, and abort's contract was already best-effort.
        session = app.ctx.uploads.pop(upload_id, None)
        if session is not None:
            await _off_loop(_unlink_quiet, [session["tmp"]])
        return sanic_json({"ok": True})

    @app.before_server_stop
    async def _drain_upload_sessions(app_, loop):
        # Unlink every in-flight upload temp on shutdown so a restart doesn't
        # leave .webterm-up-*.part litter (the lazy begin-sweep only runs while
        # the broker is up). Best-effort; the dict is cleared regardless.
        # Collect + clear ON THE LOOP (the dict is loop-owned), unlink off it.
        temps = [s["tmp"] for s in app_.ctx.uploads.values()]
        app_.ctx.uploads.clear()
        await _off_loop(_unlink_quiet, temps)

    # ---- task manager + git button (/session/*) --------------------------
    # On-demand broker<->producer round-trips (correlated by req id) so process
    # listing, scoped kill, and git status work for LOCAL and REMOTE sessions
    # alike (the agent does the work in its own host/cwd). Same token gate as
    # /launch — killing processes is privileged. The agent scopes kills
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
    # by the per-broker MCP token only — a realm entirely separate from the
    # browser auth_token: MCP is opt-in, so with no token configured (or the
    # feature disabled) the whole surface is 403 mcp_disabled. CORS rides the
    # shared response middleware; OPTIONS preflights are registered alongside.
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
                         "app_cursor": bool(s.get("app_cursor", False)),
                         # Per-terminal DEFAULT send_keys pacing (#133, set via
                         # /mcp/pace); the MCP server reads it so a no-delay_ms
                         # send auto-paces. 0 = single-burst.
                         "pace_ms": int(s.get("pace_ms", 0) or 0),
                         # #134: whether the agent has pyte. False -> read_screen
                         # uses the dependency-free textgrid fallback (no
                         # attr_runs #128 / keyframe repair #130; sparse alt-screen
                         # frames flagged partial only). Default True for a
                         # pre-#134 agent that predates the signal.
                         "pyte": bool(s.get("pyte", True))}
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
        # wait-for-idle (#135): a settle window in ms. The agent holds the reply
        # until the CURSOR-BLIND screen hash (stable_hash) has been unchanged for
        # this many ms (output went quiet), or timeout_ms elapses. Clamped like
        # timeout_ms; 0 == unset.
        try:
            wait_for_idle = int(body.get("wait_for_idle", 0) or 0)
        except (TypeError, ValueError):
            wait_for_idle = 0
        wait_for_idle = max(0, min(wait_for_idle, MAX_MCP_WAIT_MS))
        # The wait modes are exclusive (#51/#135): wait_for_change, wait_for_text,
        # wait_for_regex and wait_for_idle each pick a different signal, and
        # combining them has no well-defined meaning (which one decides
        # `matched`?). Reject up front so a caller never gets a silently-wrong
        # wait.
        n_wait = sum(bool(x) for x in
                     (wait_for_change, wait_for_text, wait_for_regex,
                      wait_for_idle))
        if n_wait > 1:
            return sanic_json(
                {"error": "conflicting_wait",
                 "detail": "use only one of wait_for_change / wait_for_text / "
                           "wait_for_regex / wait_for_idle"}, status=400)
        wait_absent = bool(body.get("wait_absent", False))
        # delta (#52): a prior content_hash; the agent returns only changed rows
        # since that frame when it can, else a full grid. Orthogonal to the wait
        # modes (it shapes the reply, not when it fires), so it does not count
        # toward the conflicting_wait check above.
        since = body.get("since")
        if not isinstance(since, str) or not since:
            since = None
        # attrs (#128): opt-in styled-run map (fg/bg/reverse), so a color-only
        # menu selection the plain text drops is visible. Orthogonal to the wait
        # and delta modes (it shapes the reply, not when it fires).
        attrs = bool(body.get("attrs", False))
        try:
            timeout_ms = int(body.get("timeout_ms", 0) or 0)
        except (TypeError, ValueError):
            timeout_ms = 0
        timeout_ms = max(0, min(timeout_ms, MAX_MCP_WAIT_MS))
        rpc_timeout = RPC_TIMEOUT
        if wait_for_change or wait_for_text or wait_for_regex or wait_for_idle:
            rpc_timeout = RPC_TIMEOUT + timeout_ms / 1000.0
        payload, error = await _session_rpc(
            entry,
            lambda req: protocol.screen_text_please_frame(
                req, view, lines, wait_for_change, timeout_ms,
                wait_for_text=wait_for_text, wait_for_regex=wait_for_regex,
                wait_absent=wait_absent, since=since, attrs=attrs,
                wait_for_idle=wait_for_idle),
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
               # stable_hash (#135): the cursor-blind digest (blink-insensitive),
               # the settle signal wait_for_idle rides. Always present (empty for
               # a degraded read or an older agent), mirroring content_hash.
               "stable_hash": str(payload.get("stable_hash", "") or ""),
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
        # attr_runs (#128): present only for an attrs read the agent could answer
        # (its pyte path); an older agent or the raw fallback omits it.
        if payload.get("attr_runs") is not None:
            out["attr_runs"] = payload.get("attr_runs")
        # idle_ms (#133): best-effort ms since the terminal last emitted PTY
        # output. Relayed only when the producer sent it (mirrors matched/
        # attr_runs): a CURRENT agent always reports it (0 == output just now),
        # an OLDER agent omits it (unknown, not a misleading 0). It is UNRELIABLE
        # for a perpetually-animating app whose idle_ms never grows.
        if payload.get("idle_ms") is not None:
            out["idle_ms"] = int(payload.get("idle_ms"))
        if payload.get("degraded"):
            out["degraded"] = True
        # partial (#130): distinct from degraded — a valid grid that may be
        # missing statically-painted panels because ring eviction lost a
        # long-running alt-screen TUI's one-time full-frame paint. Surfaced only
        # when true so a clean read stays uncluttered; the agent self-heals it.
        if payload.get("partial"):
            out["partial"] = True
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

    async def _mcp_flush(request: Request):
        # #133: discard keystrokes queued toward the app but not yet consumed
        # (a runaway send_keys backlog) — the INPUT-side mirror of /mcp/reset.
        # A mutating action (it drops pending input for the session), so it needs
        # readwrite — like /mcp/input. Same correlated round-trip as /mcp/reset:
        # only an agent answers, so a non-agent producer times out -> 502.
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        entry, mode, resp = _mcp_entry(request)
        if resp is not None:
            return resp
        if mode != "readwrite":
            return sanic_json({"error": "read_only"}, status=403)
        payload, error = await _session_rpc(
            entry, lambda req: protocol.flush_input_please_frame(req),
            "flush_input_done")
        if error is not None:
            return sanic_json({"error": "no_producer_rpc"}, status=502)
        payload = payload or {}
        if not payload.get("ok"):
            return sanic_json({"error": "flush_failed",
                               "detail": payload.get("error")}, status=502)
        return sanic_json({"ok": True, "id": entry.id})

    async def _mcp_pace(request: Request):
        # #133: set the terminal's DEFAULT inter-key pacing (ms) so a subsequent
        # send_keys that passes no delay_ms auto-paces — for a frame-polling TUI
        # (Dwarf Fortress) that drops a burst read faster than it renders. Unlike
        # /mcp/reset this is broker-LOCAL state with NO producer round-trip: it
        # just stamps entry.pace_ms, which /mcp/terminals surfaces for the MCP
        # server's client-side send_keys pacer to read. A mutating knob (it
        # changes how writes are delivered to every MCP caller of this window),
        # so it needs readwrite — like /mcp/input. The value is EPHEMERAL
        # per-connection (WindowEntry), so it resets on agent relaunch.
        err = _mcp_auth_error(request)
        if err is not None:
            return err
        entry, mode, resp = _mcp_entry(request)
        if resp is not None:
            return resp
        if mode != "readwrite":
            return sanic_json({"error": "read_only"}, status=403)
        body = _json_object_body(request) or {}
        # Reject a missing/non-numeric value (400 bad_pace, mirroring bad_id);
        # an in-range-or-not integer is CLAMPED to [0, MAX_MCP_PACE_MS] rather
        # than rejected, so 0 disables and an over-cap value pins to the cap.
        try:
            pace_ms = int(body.get("pace_ms"))
        except (TypeError, ValueError):
            return sanic_json({"error": "bad_pace"}, status=400)
        entry.pace_ms = max(0, min(pace_ms, MAX_MCP_PACE_MS))
        return sanic_json({"ok": True, "id": entry.id, "pace_ms": entry.pace_ms})

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
        params, perr = await _parse_launch_body(request)
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
    # BROWSER auth_token (same as /file/* and /state) — NOT the MCP token —
    # so the secret only ever travels to an already-
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
        # Shielded: the sidecar write awaits and the live ctx swap lands AFTER
        # it, so an unshielded cancel in that hop leaves disk ahead of memory —
        # the running broker keeps the old token/mode while a restart silently
        # adopts the abandoned one. See _shielded_region.
        async def _locked_write():
            cfg = dict(app.ctx.mcp_cfg)
            # Token edits are honored only when env is NOT pinning it (else
            # the live token would diverge from the env value restart
            # restores).
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
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=500)
            app.ctx.mcp_cfg = cfg
            return sanic_json(_mcp_cfg_public(cfg))

        return await _shielded_region(app.ctx.mcp_lock, _locked_write)

    # ---- launch profiles config (browser-facing, #70) --------------------
    # The Control Panel reads/writes the FULL profile objects here. Gated by the
    # BROWSER auth_token realm (_gated_auth_error), EXACTLY like /file/*,
    # /state and /mcp/config — never the MCP-token realm. So the commands (the
    # RCE-by-design half of the profiles-only model) only ever travel to an
    # already-authenticated browser; /profiles and /mcp/profiles stay names-only,
    # so an MCP/AI agent still can't read commands or define profiles. This
    # write is no weaker than /file/write (both grant persistent host code-exec),
    # so the browser token must be kept secret, exactly as a shell would be.
    def _which_map(exes: List[str]) -> Dict[str, bool]:
        """Resolve each executable name on PATH. BLOCKING — worker thread only.

        ``shutil.which`` walks every PATH entry (times every PATHEXT suffix on
        Windows) hitting the filesystem, and a PATH carrying a dead UNC entry
        stalls for the full SMB timeout. Takes and returns nothing but plain
        strings/bools, so it is safe to hand to the executor.
        """
        return {exe: shutil.which(exe) is not None for exe in exes}

    def _profiles_view_parts():
        """Loop-side half of the public view: pure dict shuffling, no FS.

        Returns ``(base, exe_by_name, exes)``: ``base`` is the whole response
        except ``exists``; ``exe_by_name`` maps profile name -> command[0]
        (omitted entirely for an empty command, which is the old
        ``bool(cmd)`` guard); ``exes`` is those executables DEDUPED, because
        profiles overwhelmingly share a handful of shells and the old code paid
        one PATH walk per profile instead of one per distinct executable.
        """
        launcher = app.ctx.launcher
        profiles = launcher.profiles          # live dict; iterate, never mutate
        out: Dict[str, Any] = {}
        exe_by_name: Dict[str, str] = {}
        for name, entry in profiles.items():
            cmd = list(entry.get("command") or [])
            out[name] = {"command": cmd, "title": entry.get("title"),
                         "cwd": entry.get("cwd"),
                         # #115: the editor reads this to paint each profile's
                         # default-color dot; None = no default (palette auto).
                         "color": entry.get("color")}
            if cmd:
                exe_by_name[name] = cmd[0]
        base = {"ok": True, "default_profile": launcher.default_profile,
                "profiles": out,
                "os": "windows" if os.name == "nt" else "posix",
                "source": app.ctx.profiles_source}
        return base, exe_by_name, sorted(set(exe_by_name.values()))

    async def _profiles_view_finish(parts) -> Dict[str, Any]:
        """Attach ``exists`` to a snapshot taken by ``_profiles_view_parts``.

        Split from the snapshot so a caller holding ``profiles_lock`` can grab
        its parts under the lock and RELEASE before paying for the probe —
        holding an asyncio.Lock across a possibly-stalled executor hop would
        convoy every later writer behind one dead PATH entry.
        """
        base, exe_by_name, exes = parts
        # Validate-executable-exists: does command[0] resolve on PATH now?
        # A False marks a profile whose shell isn't installed (UI red flag).
        # Deliberately NO asyncio.wait_for: a running executor future is not
        # cancellable, so a deadline would only 504 the caller while leaving
        # the thread wedged (and eventually starve the shared executor).
        found = await asyncio.get_running_loop().run_in_executor(
            None, _which_map, exes)
        base["exists"] = {name: name in exe_by_name
                          and found[exe_by_name[name]]
                          for name in base["profiles"]}
        return base

    async def _profiles_public_view() -> Dict[str, Any]:
        # Read the launcher's live state on the loop, then hand the worker only
        # inert strings — nothing loop-owned (no lock, no Request) crosses over.
        return await _profiles_view_finish(_profiles_view_parts())

    async def _profiles_config_get(request: Request):
        err = _gated_auth_error(request, "/profiles/config")
        if err is not None:
            return err
        return sanic_json(await _profiles_public_view())

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
        parts = None

        # Shielded (see _shielded_region): the sidecar write awaits and the
        # launcher live-swap lands AFTER it, so an unshielded cancel in that hop
        # keeps serving the OLD profile set from a sidecar that already holds the
        # new one — until a restart adopts the value nobody confirmed. The
        # section returns an error response, or None once the swap is done.
        async def _locked_write():
            nonlocal parts
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, app.ctx.profiles_path,
                    to_persist)
            except OSError as exc:
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=500)
            # Disk is truth first; ONLY on a successful write do we live-swap
            # the launcher, so a failed write never leaves runtime disagreeing
            # with the sidecar. set_profiles rebinds fresh objects (atomic vs
            # launch).
            app.ctx.launcher.set_profiles(result["profiles"],
                                          result["default_profile"])
            app.ctx.profiles_source = "sidecar"
            # Snapshot the response set while STILL holding the lock, so this
            # client is answered with the profiles IT just wrote. Sampling the
            # launcher after the release would let a concurrent POST land in
            # between and hand back someone else's set — which the editor
            # would then save straight back, silently reverting the other
            # write. Only the snapshot is under the lock; the blocking PATH
            # probe below is not (and is left UNSHIELDED — it is a pure read
            # with no side effect worth finishing for a gone client).
            parts = _profiles_view_parts()
            return None

        write_err = await _shielded_region(app.ctx.profiles_lock, _locked_write)
        if write_err is not None:
            return write_err
        # Audit: this write persists shell recipes /launch will spawn by name.
        LOGGER.info("launch profiles updated via /profiles/config: %d "
                    "profiles (default=%r) from %s", len(result["profiles"]),
                    result["default_profile"], request.ip)
        return sanic_json(await _profiles_view_finish(parts))

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
    # Non-secret stable id + build version (#64). Gated by the SAME token
    # policy as /state: the same-origin local probe passes via the local host's
    # stored token, and the cross-origin add-time probe via the ?token=
    # appendHostToken already attaches. Gating (vs fully public) keeps a
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

    # ---- AI-provider status proxy (/status/fetch, #112) ------------------
    # The broker's ONLY outbound HTTP. Gated by the SAME token
    # policy as /info & /state (browser realm). The client passes ONLY allowlist
    # ids (?provider=a,b — never a URL); unknown ids are dropped, and a request
    # that names providers but validates NONE is a 400 (the SSRF allowlist
    # proof). Absent/empty ?provider => all providers. Results are per-id cached
    # (STATUS_CACHE_TTL) and the upstream fetch itself is https-only + no-redirect
    # + size-capped (see _fetch_status_blocking). Never blocks: a dead provider
    # degrades to an "unknown" row.
    async def _status_one(pid: str) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        cache = app.ctx.status_cache
        hit = cache.get(pid)
        if hit is not None and (loop.time() - hit["at"]) < STATUS_CACHE_TTL:
            return hit["data"]
        data = await loop.run_in_executor(None, _fetch_status_blocking, pid)
        cache[pid] = {"at": loop.time(), "data": data}
        return data

    async def _status_fetch(request: Request):
        err = _gated_auth_error(request, "/status/fetch")
        if err is not None:
            return err
        # Collect non-empty, comma-split tokens across any ?provider= params.
        provided = request.args.getlist("provider")
        tokens = [s.strip() for chunk in provided
                  for s in chunk.split(",") if s.strip()]
        if not tokens:
            ids = list(STATUS_ALLOWLIST.keys())          # empty/absent => all
        else:
            ids = []
            for t in tokens:
                # Validate against the allowlist; DROP unknowns (never a URL).
                if t in STATUS_ALLOWLIST and t not in ids:
                    ids.append(t)
            if not ids:                                  # named some, matched none
                return sanic_json({"ok": False, "error": "no_valid_provider"},
                                  status=400)
        results = await asyncio.gather(*[_status_one(pid) for pid in ids])
        return sanic_json({"ok": True, "fetchedAt": int(time.time()),
                           "providers": list(results)})

    # ---- shared UI state (/state) ----------------------------------------
    # Per-broker settings + layout, shared across a user's browsers. Optimistic
    # concurrency on an integer rev: GET returns {rev, settings, layout}; PUT
    # supplies {baseRev, settings, layout} and is rejected 409 (with the
    # current state inlined, so the loser resyncs in one round trip) when
    # baseRev != the live rev. Same token gate as /file/*.
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
        #
        # Shielded for the same reason (see _shielded_region), with a rev-
        # specific payoff: the write awaits and `ctx.state = new_state` lands
        # AFTER it, so an unshielded cancel there leaves DISK AHEAD OF MEMORY.
        # It self-heals on the next successful PUT, but a restart inside that
        # window silently adopts a value no client was ever told landed, and the
        # same rev number ends up describing two different states.
        async def _locked_write():
            # Single-active-client lease: a non-active browser must not mutate
            # the shared layout/settings (a torn-down/background tab could
            # otherwise clobber the active one). 409 not_active inlines the
            # live state so the loser resyncs in one round trip. A None lease
            # (broker just restarted, nobody has claimed yet) does NOT block —
            # and GET /state stays ungated so a reactivating tab can read.
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
                # Conflict — inline the live state so the client rebases
                # without a second GET (Codex review fix: avoids a 409 retry
                # storm).
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
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=500)
            app.ctx.state = new_state
            return sanic_json({"ok": True, "rev": new_state["rev"]})

        return await _shielded_region(app.ctx.state_lock, _locked_write)

    # ---- /mod-store/<modId> (#124): generic per-mod server KV + rev ring ----
    # The durable, cross-browser twin of ctx.storage. Same token gate
    # and the SAME single-active-client lease as /state. GET is ungated by the
    # lease (a non-active/reactivating browser can always READ); PUT is lease-
    # gated (409 not_active) so a background tab can't clobber the active one.
    def _modstore_bad_id(mod_id: str):
        """Validate the <modId> path segment; a bad id -> 400 (else None)."""
        if not _MODSTORE_ID_RE.fullmatch(mod_id or ""):
            return sanic_json({"ok": False, "error": "bad_mod_id"}, status=400)
        return None

    async def _modstore_get(request: Request, modId: str):
        err = _gated_auth_error(request, "/mod-store")
        if err is not None:
            return err
        bad = _modstore_bad_id(modId)
        if bad is not None:
            return bad
        rec = app.ctx.modstore.get(modId) \
            or {"rev": 0, "value": None, "revisions": []}
        # ?rev=<n>: return that ONE revision's full value — the current rev, or a
        # ring entry. Absent from both -> 404 (it scrolled off the ring); a non-
        # int -> 400. This is how History previews/restores a past value.
        rev_q = request.args.get("rev")
        if rev_q is not None:
            try:
                want = int(rev_q)
            except (TypeError, ValueError):
                return sanic_json({"ok": False, "error": "bad_rev"}, status=400)
            if want == rec["rev"]:
                return sanic_json({"ok": True, "rev": want, "value": rec["value"]})
            for ent in rec["revisions"]:
                if ent["rev"] == want:
                    return sanic_json({"ok": True, "rev": want,
                                       "value": ent["value"]})
            return sanic_json({"ok": False, "error": "no_such_rev"}, status=404)
        # Default: current value + revision METADATA only (rev + ts, no bodies —
        # the ring can be large; History fetches a body on demand via ?rev=).
        return sanic_json({
            "rev": rec["rev"],
            "value": rec["value"],
            "revisions": [{"rev": e["rev"], "ts": e["ts"]}
                          for e in rec["revisions"]],
        })

    async def _modstore_put(request: Request, modId: str):
        err = _gated_auth_error(request, "/mod-store")
        if err is not None:
            return err
        bad = _modstore_bad_id(modId)
        if bad is not None:
            return bad
        if request.body and len(request.body) > MAX_MODSTORE_BYTES:
            return sanic_json({"ok": False, "error": "too_large"}, status=413)
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        base_rev = body.get("baseRev")
        # Strict int: bool is an int subclass in Python, so reject True/False
        # (they'd otherwise pass as 1/0 and silently mis-compare against rev).
        if isinstance(base_rev, bool) or not isinstance(base_rev, int) \
                or base_rev < 0:
            return sanic_json({"ok": False, "error": "bad_baseRev"}, status=400)
        # Presence check, not truthiness: value:null is a legal payload (clearing
        # every note). _json_object_body maps an empty body to {} -> 400 here.
        if "value" not in body:
            return sanic_json({"ok": False, "error": "bad_value"}, status=400)
        value = body["value"]
        # Optional "write this value AND forget the history" flag (#65). Strict
        # bool, mirroring the baseRev bool-is-an-int guard above: a purge must be
        # an explicit True, never a stray 1/"true"/{} that slipped through. When
        # set, no prior value is pushed and the revision ring is written empty —
        # the only way to un-publish a value that scrolled into the ring (e.g. a
        # host token the registry mod published, then revoked). value:null +
        # purgeRevisions:true = "forget everything this mod stored".
        purge = body.get("purgeRevisions", False)
        if not isinstance(purge, bool):
            return sanic_json({"ok": False, "error": "bad_purgeRevisions"},
                              status=400)
        client_id = str(body.get("clientId") or "").strip()
        # Lock the whole lease-check / read-rev / compare / dedupe / write / bump
        # (identical reasoning to /state: the write awaits, so two PUTs could
        # interleave on rev, and the lease check must live INSIDE the lock so a
        # become_active that linearized while this PUT queued is seen).
        # Shielded exactly like /state, and for the same disk-ahead-of-memory
        # reason — see _shielded_region.
        async def _locked_write():
            existed = modId in app.ctx.modstore
            rec = app.ctx.modstore.get(modId) \
                or {"rev": 0, "value": None, "revisions": []}
            active = app.ctx.active_client_id
            if active is not None and client_id != active:
                return sanic_json({
                    "ok": False, "error": "not_active",
                    "rev": rec["rev"], "value": rec["value"],
                }, status=409)
            if base_rev != rec["rev"]:
                # Conflict — inline the live value so the client rebases in
                # one round trip (no follow-up GET), matching /state.
                return sanic_json({
                    "ok": False, "error": "conflict",
                    "rev": rec["rev"], "value": rec["value"],
                }, status=409)
            # No-op dedupe: an idle debounced autosave that resends the
            # current value must NOT bump rev or push a revision (else the
            # ring churns on every keystroke pause). Accept it as a success at
            # the current rev. A purge is NOT a no-op when there is still a
            # ring to clear: dropping history is an OBSERVABLE mutation (GET
            # ?rev=<old> flips 200->404), so it must be visible as a new rev —
            # it falls through to the write below (which bumps rev and writes
            # revisions:[]) rather than silently mutating the current rev in
            # place. The ONLY true no-op is a purge with nothing left to
            # clear: value unchanged AND the ring already empty (e.g. a
            # first-write dedupe on the empty seed).
            if value == rec["value"] and not (purge and rec["revisions"]):
                return sanic_json({"ok": True, "rev": rec["rev"]})
            # Push the OUTGOING (soon-to-be-prior) value onto the newest-first
            # ring, then trim. Skip the empty seed: a brand-new mod id has no
            # real prior value (rev 0 / None), so don't record a meaningless
            # {rev:0} entry — the ring starts once there's genuine history. A
            # purge writes the ring EMPTY instead (drop the outgoing prior
            # too), so the new value lands with no recoverable history.
            if purge:
                revisions = []
            else:
                revisions = list(rec["revisions"])
                if existed:
                    revisions.insert(0, {"rev": rec["rev"],
                                         "value": rec["value"],
                                         "ts": int(time.time())})
                    del revisions[MODSTORE_MAX_REVISIONS:]
            new_rec = {"rev": rec["rev"] + 1, "value": value,
                       "revisions": revisions}
            new_store = dict(app.ctx.modstore)
            new_store[modId] = new_rec
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, app.ctx.modstore_path,
                    new_store)
            except OSError as exc:
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=500)
            app.ctx.modstore = new_store
            return sanic_json({"ok": True, "rev": new_rec["rev"]})

        return await _shielded_region(app.ctx.modstore_lock, _locked_write)

    # ---- terminal session recordings (/recording/*, #140) ----------------
    # The recorder mod's storage: a finished recording streams in through the
    # begin/chunk/commit trio below (server-generated rec-* ids — the client
    # never names a path, strictly narrower than /file/upload_* exactly like
    # /file/paste_image), lands atomically as <id>.blrec.gz in recordings_dir
    # (#159 — gzip, one member per uploaded chunk; <id>.blrec is the older
    # uncompressed form and stays readable forever), with a whitelisted meta
    # sidecar (<id>.meta.json) so listing never parses the big event file, and
    # a revisioned notes sidecar (<id>.notes.json). Same token gate as /file/*.
    def _rec_auth_error(request: Request):
        return _gated_auth_error(request, "/recording")

    def _rec_paths(rec_id: str) -> Optional[_RecPaths]:
        """Every path for a VALIDATED id, or None on a bad id. The strict id
        regex is the whole traversal defense: every file touched is
        <recordings_dir>/<id><fixed suffix>, for BOTH event-file suffixes."""
        if not isinstance(rec_id, str) or not _RECORDING_ID_RE.fullmatch(rec_id):
            return None
        d = app.ctx.recordings_dir
        return _RecPaths(rec_id,
                         d / (rec_id + REC_SUFFIX_GZ), d / (rec_id + REC_SUFFIX),
                         d / (rec_id + ".meta.json"),
                         d / (rec_id + ".notes.json"))

    async def _recording_begin(request: Request):
        err = _rec_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rec_dir = app.ctx.recordings_dir
        # mkdir(mode, parents, exist_ok) positionally — 0o700 on POSIX, the
        # default 0o777 (ignored by Windows) elsewhere, exactly as before.
        mode = 0o700 if os.name == "posix" else 0o777
        try:
            await _off_loop(rec_dir.mkdir, mode, True, True)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        # Reap ON THE LOOP then check the cap with no await between them, and do
        # the unlinks + orphan scan in a worker — see _file_upload_begin for why.
        now = time.time()
        stale = _reap_rec_sessions(app.ctx.rec_uploads, now)
        over_cap = len(app.ctx.rec_uploads) >= MAX_RECORDING_SESSIONS
        await _off_loop(_sweep_rec_temps, stale, rec_dir, now)
        if over_cap:
            return sanic_json({"ok": False, "error": "too_many_sessions"},
                              status=429)
        try:
            tmp = await _off_loop(_mkstemp_part, rec_dir, ".webterm-rec-")
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        # Authoritative cap check adjacent to the insert — see _file_upload_begin.
        if len(app.ctx.rec_uploads) >= MAX_RECORDING_SESSIONS:
            await _off_loop(_unlink_quiet, [tmp])
            return sanic_json({"ok": False, "error": "too_many_sessions"},
                              status=429)
        rec_id = "rec-%s-%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                secrets.token_hex(4))
        app.ctx.rec_uploads[rec_id] = {
            "tmp": tmp, "received": 0, "created": now,
            # Same per-session lock as an upload session, for the same reason and
            # with the same lifetime: it lives in this dict, so popping the
            # session is what frees it. See _append_chunk.
            "lock": asyncio.Lock(),
        }
        return sanic_json({"ok": True, "recording_id": rec_id})

    async def _recording_chunk(request: Request):
        # Append ONE chunk to an in-flight save — the same sequential-offset /
        # size-cap / drop-on-error contract as /file/upload_chunk, with the
        # recording-specific MAX_RECORDING_BYTES ceiling.
        err = _rec_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rec_id = body.get("recording_id")
        b64 = body.get("content_b64")
        offset = body.get("offset", 0)
        if not isinstance(rec_id, str) or not isinstance(b64, str):
            return sanic_json({"ok": False, "error": "bad_request"}, status=400)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return sanic_json({"ok": False, "error": "bad_offset"}, status=400)
        session = app.ctx.rec_uploads.get(rec_id)
        if session is None:
            return sanic_json({"ok": False, "error": "no_session"}, status=404)
        try:
            data = base64.b64decode(b64, validate=True)
        except (ValueError, base64.binascii.Error):
            return sanic_json({"ok": False, "error": "bad_base64"}, status=400)
        if len(data) > MAX_CHUNK_BYTES:
            return sanic_json({"ok": False, "error": "chunk_too_large"},
                              status=400)
        # Identical atomic region to /file/upload_chunk, minus the digest — the
        # per-session lock is what keeps guard -> append -> accounting indivisible
        # now that the append rides a worker, and the shield is what keeps a
        # disconnect from unwinding the region (releasing the lock) while that
        # worker is still inside the append. See _append_chunk / _shielded_region.
        async def _locked_append():
            if app.ctx.rec_uploads.get(rec_id) is not session:
                return sanic_json({"ok": False, "error": "no_session"},
                                  status=404)
            if offset != session["received"]:
                return sanic_json({"ok": False, "error": "bad_offset",
                                   "received": session["received"]},
                                  status=409)
            if session["received"] + len(data) > MAX_RECORDING_BYTES:
                app.ctx.rec_uploads.pop(rec_id, None)
                await _off_loop(_unlink_quiet, [session["tmp"]])
                return sanic_json({"ok": False, "error": "too_large"},
                                  status=400)
            try:
                # #159: each chunk becomes its own gzip member. `received`
                # still counts DECODED INPUT bytes, so MAX_RECORDING_BYTES
                # keeps its exact meaning and the client's roll threshold is
                # untouched.
                await _off_loop(_append_chunk_gz, session["tmp"], data)
            except (OSError, zlib.error) as exc:
                app.ctx.rec_uploads.pop(rec_id, None)
                await _off_loop(_unlink_quiet, [session["tmp"]])
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=400)
            session["received"] += len(data)
            return sanic_json({"ok": True,
                               "received": session["received"]})

        return await _shielded_region(session["lock"], _locked_append)

    async def _recording_commit(request: Request):
        # Finalize: atomic os.replace of the temp onto <id>.blrec.gz, then the
        # whitelisted meta sidecar (with both sizes — see below). A failed
        # sidecar write rolls the event file back off disk so list/get never
        # see a half-registered recording.
        err = _rec_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rec_id = body.get("recording_id")
        if not isinstance(rec_id, str):
            return sanic_json({"ok": False, "error": "bad_request"}, status=400)
        session = app.ctx.rec_uploads.get(rec_id)
        if session is None:
            return sanic_json({"ok": False, "error": "no_session"}, status=404)
        paths = _rec_paths(rec_id)
        if paths is None:
            return sanic_json({"ok": False, "error": "bad_id"}, status=400)
        # A new recording is ALWAYS compressed — never "whichever exists".
        blrec, meta_path = paths.gz, paths.meta
        meta = _rec_sanitize_meta(body.get("meta"))
        # Same per-session lock as /file/upload_commit: the size measurement, the
        # sidecar write and the replace now span awaits, and an append landing in
        # between would put bytes in the .blrec that meta["size"] never counted.
        #
        # Shielded (see _shielded_region), and this is the site where it matters
        # most: the replace is the LAST await, so an unshielded cancel inside it
        # publishes and lists the recording while the pop below never runs. The
        # user is told "not saved" for a recording that is on disk, complete, and
        # the session keeps one of MAX_RECORDING_SESSIONS slots for an hour.
        # Shielding makes "published" and "session popped" happen together, which
        # also removes the precondition for the second-commit cleanup below.
        async def _locked_commit():
            if app.ctx.rec_uploads.get(rec_id) is not session:
                return sanic_json({"ok": False, "error": "no_session"},
                                  status=404)
            tmp = session["tmp"]
            # Meta sidecar FIRST, .blrec replace SECOND: listing keys off
            # .blrec presence, so this order means a listed recording always
            # has its sidecar — a crash between the two leaves only an orphan
            # sidecar (invisible, harmless), never a half-registered
            # recording.
            try:
                # `size` keeps meaning UNCOMPRESSED bytes — the length of the
                # JSONL the client uploaded, which is what it meant for every
                # recording before #159 and what a player/download produces.
                # It comes from the accounted input bytes, NOT from stat, so
                # compression cannot silently redefine the recorder's size
                # column mid-library. `diskSize` is the new, separately named
                # on-disk footprint.
                size = session["received"]
                meta["size"] = size
                meta["enc"] = "gzip"
                meta["diskSize"] = await _off_loop(os.path.getsize, tmp)
                meta["savedAt"] = int(time.time())
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, meta_path, meta)
                await _off_loop(os.replace, tmp, str(blrec))
            except OSError as exc:
                app.ctx.rec_uploads.pop(rec_id, None)
                await _off_loop(_unlink_quiet, [tmp, str(meta_path)])
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=400)
            app.ctx.rec_uploads.pop(rec_id, None)
            return sanic_json({"ok": True, "recording_id": rec_id, "size": size})

        return await _shielded_region(session["lock"], _locked_commit)

    async def _recording_abort(request: Request):
        # Idempotent cleanup, mirroring /file/upload_abort.
        err = _rec_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rec_id = body.get("recording_id")
        if not isinstance(rec_id, str):
            return sanic_json({"ok": False, "error": "bad_request"}, status=400)
        # Lock-free pop for the same reason as /file/upload_abort: abort must
        # never queue behind a wedged append.
        session = app.ctx.rec_uploads.pop(rec_id, None)
        if session is not None:
            await _off_loop(_unlink_quiet, [session["tmp"]])
        return sanic_json({"ok": True})

    async def _recordings_list(request: Request):
        err = _rec_auth_error(request)
        if err is not None:
            return err
        rec_dir = app.ctx.recordings_dir

        def _scan():
            out = []
            seen = set()
            try:
                # TWO exact globs, gzip first, never one loose "rec-*.blrec*":
                # that would also sweep up editor backups and stray temps. Note
                # `rec-*.blrec` does NOT match `<id>.blrec.gz` — fnmatch anchors
                # the end — so the two lists are disjoint by construction.
                files = (sorted(rec_dir.glob("rec-*" + REC_SUFFIX_GZ))
                         + sorted(rec_dir.glob("rec-*" + REC_SUFFIX)))
            except OSError:
                return out
            for f in files:
                gz = f.name.endswith(REC_SUFFIX_GZ)
                rec_id = f.name[:-len(REC_SUFFIX_GZ if gz else REC_SUFFIX)]
                # gzip is scanned first, so a duplicated id resolves to the
                # compressed file here exactly as it does in get/notes/delete.
                if not _RECORDING_ID_RE.fullmatch(rec_id) or rec_id in seen:
                    continue
                seen.add(rec_id)
                meta = _rec_load_json(
                    rec_dir / (rec_id + ".meta.json")) or {}
                notes = _rec_load_notes(rec_dir / (rec_id + ".notes.json"))
                try:
                    disk = f.stat().st_size
                except OSError:
                    disk = None
                logical = meta.get("size")
                if isinstance(logical, bool) or not isinstance(logical, int) \
                        or logical < 0:
                    logical = None
                entry = {"id": rec_id,
                         "notesCount": len(notes["notes"]),
                         "notesRev": notes["rev"]}
                if disk is not None:
                    entry["diskSize"] = disk       # always the CURRENT stat,
                    #   never meta["diskSize"] — the file on disk is the truth
                if gz:
                    entry["enc"] = "gzip"
                    # No stat fallback for `size` here: stat is the COMPRESSED
                    # length, and reporting it under a field that means
                    # uncompressed bytes would understate every recording whose
                    # sidecar went missing. Better absent (the client renders
                    # it unknown, alongside the ?×? and 0:00 that a lost sidecar
                    # already produces) than confidently wrong.
                    if logical is not None:
                        entry["size"] = logical
                else:
                    # Uncompressed: stat IS the uncompressed length, and stays
                    # authoritative over the sidecar exactly as before #159.
                    entry["size"] = disk if disk is not None else (logical or 0)
                for key in ("title", "cols", "rows", "startedAt",
                            "durationMs", "events", "fontFamily", "fontSize",
                            "savedAt", "series", "seg"):
                    if key in meta:
                        entry[key] = meta[key]
                out.append(entry)
            out.sort(key=lambda e: e.get("startedAt", 0), reverse=True)
            return out

        recordings = await asyncio.get_running_loop().run_in_executor(
            None, _scan)
        return sanic_json({"ok": True, "recordings": recordings})

    async def _recording_get(request: Request):
        err = _rec_auth_error(request)
        if err is not None:
            return err
        paths = _rec_paths(request.args.get("id"))
        if paths is None:
            return sanic_json({"ok": False, "error": "bad_id"}, status=400)
        # ALWAYS plain JSONL on the wire, whatever the storage encoding is.
        #
        # #159 originally planned to hand a stored .blrec.gz straight through
        # under `Content-Encoding: gzip` and let the client inflate — less work
        # here, fewer bytes on the wire. DO NOT REINSTATE THAT without changing
        # how the file is FRAMED. Chunks are stored as separate gzip MEMBERS,
        # and a concatenated-member stream, while perfectly valid gzip, is not
        # universally handled by HTTP client decoders: they commonly wrap a bare
        # `zlib.decompressobj(wbits=31)`, which stops at the end of the first
        # member and reports success. httpx does exactly this (verified — its
        # GZipDecoder returns only the first member and drops the rest), so a
        # multi-chunk recording would arrive SILENTLY TRUNCATED, which for an
        # archive is the worst possible failure. Serving decoded bytes keeps
        # this endpoint byte-identical to its pre-#159 behaviour for every
        # client, and the issue's own framing is that disk is the growth
        # problem and the wire is not.
        def _read():
            """Resolve the encoding by SUFFIX and return decoded JSONL.

            ONE worker hop for the whole read, and the inflate happens in here
            too, so the compressed buffer is dropped on return rather than
            being held alongside the decoded one on the loop."""
            for path, stored_gz in ((paths.gz, True), (paths.raw, False)):
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    continue
                return gzip.decompress(data) if stored_gz else data
            raise FileNotFoundError(str(paths.gz))

        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, _read)
        except FileNotFoundError:
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        except (OSError, EOFError, zlib.error) as exc:
            # BadGzipFile is an OSError, but a TRUNCATED member raises EOFError
            # and a corrupt deflate stream raises zlib.error — neither is one,
            # and an uncaught one here would be a 500 on a damaged archive.
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        # The name is built from the validated id, not from the file's stem:
        # Path("<id>.blrec.gz").stem is "<id>.blrec", which would have produced
        # "<id>.blrec.blrec".
        return sanic_raw(data, content_type="application/octet-stream",
                         headers={"Content-Disposition":
                                  'attachment; filename="%s%s"'
                                  % (paths.rec_id, REC_SUFFIX)})

    async def _recording_delete(request: Request):
        # The ONLY deletion path for a committed recording (no sweep ever).
        # Unlinks the event file first, then the sidecars best-effort, so a
        # partial failure can orphan a sidecar but never a listed recording.
        err = _rec_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        paths = _rec_paths(body.get("id"))
        if paths is None:
            return sanic_json({"ok": False, "error": "bad_id"}, status=400)

        def _unlink_all():
            # ONE hop for the whole teardown. The event files go first and their
            # failure is the reported one (nothing removed -> 404, any other
            # OSError -> 400); the sidecars stay best-effort, and are not
            # touched at all when an event-file unlink raises, exactly as when
            # these were three separate on-loop calls.
            #
            # BOTH suffixes are unlinked (#159), so deleting a recording never
            # leaves the other encoding behind to reappear in the next listing.
            # A non-missing OSError still propagates immediately: the recording
            # is not gone, so its sidecars must survive to keep it listable.
            removed = 0
            for path in paths.events:
                try:
                    os.unlink(str(path))
                    removed += 1
                except FileNotFoundError:
                    continue
            if not removed:
                raise FileNotFoundError(str(paths.gz))
            for side in (paths.meta, paths.notes):
                try:
                    os.unlink(str(side))
                except OSError:
                    pass

        try:
            await _off_loop(_unlink_all)
        except FileNotFoundError:
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True})

    async def _recording_notes_get(request: Request):
        err = _rec_auth_error(request)
        if err is not None:
            return err
        paths = _rec_paths(request.args.get("id"))
        if paths is None:
            return sanic_json({"ok": False, "error": "bad_id"}, status=400)
        def _read():
            # Existence probe + sidecar parse in ONE hop off the loop. A
            # deleted/never-saved recording must not look note-valid via an
            # orphan sidecar — mirror the PUT's existence check. EITHER
            # encoding counts: an old uncompressed recording is annotatable.
            if not any(p.exists() for p in paths.events):
                return None
            return _rec_load_notes(paths.notes)

        notes = await _off_loop(_read)
        if notes is None:
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        return sanic_json({"ok": True, "rev": notes["rev"],
                           "notes": notes["notes"]})

    async def _recording_notes_put(request: Request):
        # Whole-list replace under optimistic concurrency (baseRev), matching
        # /state and /mod-store: a stale writer gets 409 with the live value so
        # a second player window can't silently drop the first one's notes.
        # Note times are clamped to [0, durationMs] when the meta knows it.
        err = _rec_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        paths = _rec_paths(body.get("id"))
        if paths is None:
            return sanic_json({"ok": False, "error": "bad_id"}, status=400)
        base_rev = body.get("baseRev")
        if isinstance(base_rev, bool) or not isinstance(base_rev, int) \
                or base_rev < 0:
            return sanic_json({"ok": False, "error": "bad_baseRev"},
                              status=400)
        raw_notes = body.get("notes")
        if not isinstance(raw_notes, list) \
                or len(raw_notes) > MAX_RECORDING_NOTES:
            return sanic_json({"ok": False, "error": "bad_notes"}, status=400)

        def _read_meta():
            # Existence probe + meta parse in ONE hop off the loop. None means
            # "no such recording"; a missing/corrupt meta sidecar still
            # degrades to {} the way it always did. Either encoding counts.
            if not any(p.exists() for p in paths.events):
                return None
            return _rec_load_json(paths.meta) or {}

        meta = await _off_loop(_read_meta)
        if meta is None:
            return sanic_json({"ok": False, "error": "not_found"}, status=404)
        duration = meta.get("durationMs")
        clean = []
        for n in raw_notes:
            if not isinstance(n, dict):
                return sanic_json({"ok": False, "error": "bad_notes"},
                                  status=400)
            t = n.get("t")
            text = n.get("text")
            if isinstance(t, bool) or not isinstance(t, (int, float)) \
                    or not isinstance(text, str) \
                    or (isinstance(t, float) and not math.isfinite(t)):
                return sanic_json({"ok": False, "error": "bad_notes"},
                                  status=400)
            if len(text) > MAX_RECORDING_NOTE_TEXT:
                return sanic_json({"ok": False, "error": "note_too_long"},
                                  status=400)
            t = max(0, int(t))
            if isinstance(duration, int) and duration >= 0:
                t = min(t, duration)
            clean.append({"t": t, "text": text})
        clean.sort(key=lambda n: n["t"])
        # Shielded (see _shielded_region). This is the ONLY read-compare-write in
        # the tree whose compare reads DISK rather than loop-owned memory, which
        # makes the lock the entire serialisation: a cancel between the load and
        # the write releases it, lets a second editor read the SAME rev, and the
        # first writer's replace can still land afterwards — a silent lost update
        # that both clients were told succeeded.
        async def _locked_write():
            # The lock MUST span the whole read -> compare -> write sequence,
            # or two concurrent note edits interleave and the loser's notes
            # are silently overwritten. Both the read and the write are now
            # off the loop, so the critical section holds across two await
            # points — that is the point of the lock, not a bug to "fix" by
            # narrowing it. rec_notes_lock is an asyncio.Lock (loop-affine):
            # it is acquired and released on the loop and never crosses into
            # a worker; only the inert notes path and plain dict do.
            current = await _off_loop(_rec_load_notes, paths.notes)
            if base_rev != current["rev"]:
                return sanic_json({"ok": False, "error": "conflict",
                                   "rev": current["rev"],
                                   "notes": current["notes"]}, status=409)
            new_rec = {"rev": current["rev"] + 1, "notes": clean}
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _write_state_atomic, paths.notes, new_rec)
            except OSError as exc:
                return sanic_json({"ok": False, "error": str(exc)},
                                  status=500)
            return sanic_json({"ok": True, "rev": new_rec["rev"]})

        return await _shielded_region(app.ctx.rec_notes_lock, _locked_write)

    @app.before_server_stop
    async def _drain_rec_sessions(app_, loop):
        # Unlink every in-flight recording-save temp on shutdown, mirroring
        # _drain_upload_sessions (collect + clear on the loop, unlink off it).
        # Committed recordings are untouched.
        temps = [s["tmp"] for s in app_.ctx.rec_uploads.values()]
        app_.ctx.rec_uploads.clear()
        await _off_loop(_unlink_quiet, temps)

    if app.ctx.serve_ui:
        # Import (and thus assemble) the UI constants here, gated on serve_ui, so
        # a headless broker never reads the NN_* fragments or the wiki. Assembly
        # at create_app time preserves UI mode's loud-at-startup failure for a
        # missing/oversized fragment (ui.assemble() is non-protective); deferring
        # it into the handler would let a broken broker boot "healthy" and only
        # 500 on the first GET /. sys.modules caches the assembled values.
        from .ui import INDEX_HTML, inline_script_hash
        from .help_corpus import HELP_CORPUS
        from . import vendor
        app.ctx.index_html = INDEX_HTML
        app.ctx.help_corpus = HELP_CORPUS
        # #143: authorize OUR bundle by hash so script-src needs no
        # 'unsafe-inline'. Computed from the assembled page, here rather than at
        # module scope so a headless broker still never imports .ui (#87).
        app.ctx.csp = _csp_header(inline_script_hash(INDEX_HTML))
        # Vendored xterm, read eagerly for the same loud-at-startup reason the
        # UI is assembled here (#87 keeps both out of a headless broker).
        app.ctx.vendor = vendor.load()
        LOGGER.info("vendored assets: %d (%s)", len(app.ctx.vendor),
                    vendor.URL_PREFIX)
        app.add_route(_index, "/", methods=["GET"])
        app.add_route(_help_corpus, "/help-corpus.json", methods=["GET"])
        app.add_route(_vendor_asset, vendor.URL_PREFIX + "<name:str>",
                      methods=["GET"])
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
    app.add_route(_file_hash, "/file/hash", methods=["POST"])
    app.add_route(_file_write, "/file/write", methods=["POST"])
    app.add_route(_file_upload, "/file/upload", methods=["POST"])
    app.add_route(_file_upload_begin, "/file/upload_begin", methods=["POST"])
    app.add_route(_file_upload_chunk, "/file/upload_chunk", methods=["POST"])
    app.add_route(_file_upload_commit, "/file/upload_commit", methods=["POST"])
    app.add_route(_file_upload_abort, "/file/upload_abort", methods=["POST"])
    app.add_route(_file_paste_image, "/file/paste_image", methods=["POST"])
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
    app.add_route(_status_fetch, "/status/fetch", methods=["GET"])
    app.add_route(_state_get, "/state", methods=["GET"])
    app.add_route(_state_put, "/state", methods=["PUT"])
    app.add_route(_modstore_get, "/mod-store/<modId>", methods=["GET"])
    app.add_route(_modstore_put, "/mod-store/<modId>", methods=["PUT"])
    # Terminal session recordings (#140).
    app.add_route(_recording_begin, "/recording/begin", methods=["POST"])
    app.add_route(_recording_chunk, "/recording/chunk", methods=["POST"])
    app.add_route(_recording_commit, "/recording/commit", methods=["POST"])
    app.add_route(_recording_abort, "/recording/abort", methods=["POST"])
    app.add_route(_recordings_list, "/recordings", methods=["GET"])
    app.add_route(_recording_get, "/recording", methods=["GET"])
    app.add_route(_recording_delete, "/recording/delete", methods=["POST"])
    app.add_route(_recording_notes_get, "/recording/notes", methods=["GET"])
    app.add_route(_recording_notes_put, "/recording/notes", methods=["POST"])
    # MCP HTTP interface (external MCP server) + its browser-facing config.
    app.add_route(_mcp_info, "/mcp/info", methods=["GET"])
    app.add_route(_mcp_terminals, "/mcp/terminals", methods=["GET"])
    app.add_route(_mcp_read, "/mcp/read", methods=["POST"])
    app.add_route(_mcp_input, "/mcp/input", methods=["POST"])
    app.add_route(_mcp_reset, "/mcp/reset", methods=["POST"])
    app.add_route(_mcp_flush, "/mcp/flush", methods=["POST"])
    app.add_route(_mcp_pace, "/mcp/pace", methods=["POST"])
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
                             ("/file/hash", "preflight_file_hash"),
                             ("/file/write", "preflight_file_write"),
                             ("/file/upload", "preflight_file_upload"),
                             ("/file/upload_begin", "preflight_file_upload_begin"),
                             ("/file/upload_chunk", "preflight_file_upload_chunk"),
                             ("/file/upload_commit", "preflight_file_upload_commit"),
                             ("/file/upload_abort", "preflight_file_upload_abort"),
                             ("/file/paste_image", "preflight_file_paste_image"),
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
                             ("/status/fetch", "preflight_status_fetch"),
                             ("/state", "preflight_state"),
                             ("/mod-store/<modId>", "preflight_mod_store"),
                             ("/recording/begin", "preflight_rec_begin"),
                             ("/recording/chunk", "preflight_rec_chunk"),
                             ("/recording/commit", "preflight_rec_commit"),
                             ("/recording/abort", "preflight_rec_abort"),
                             ("/recordings", "preflight_recordings"),
                             ("/recording", "preflight_recording"),
                             ("/recording/delete", "preflight_rec_delete"),
                             ("/recording/notes", "preflight_rec_notes"),
                             ("/mcp/info", "preflight_mcp_info"),
                             ("/mcp/terminals", "preflight_mcp_terminals"),
                             ("/mcp/read", "preflight_mcp_read"),
                             ("/mcp/input", "preflight_mcp_input"),
                             ("/mcp/reset", "preflight_mcp_reset"),
                             ("/mcp/flush", "preflight_mcp_flush"),
                             ("/mcp/pace", "preflight_mcp_pace"),
                             ("/mcp/profiles", "preflight_mcp_profiles"),
                             ("/mcp/launch", "preflight_mcp_launch"),
                             ("/mcp/config", "preflight_mcp_config"),
                             ("/profiles/config", "preflight_profiles_config"),
                             ("/profiles/detect", "preflight_profiles_detect")):
        app.add_route(_preflight, path, methods=["OPTIONS"], name=route_name)
    app.error_handler.add(NotFound, _handle_404)

    return app


