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
import json
import logging
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from sanic import Request, Sanic, Websocket
from sanic.exceptions import NotFound
from sanic.response import empty, html, json as sanic_json

from .. import build_version, protocol
from . import auth, relay
from .launcher import LaunchError, Launcher
from .registry import BrokerRegistry, run_producer_session
from .help_corpus import HELP_CORPUS
from .ui import INDEX_HTML

LOGGER = logging.getLogger(__name__)

CONFIG_ENV = "WEB_TERMINAL_CONFIG"
DEFAULT_PORT = 4445

# Editor file-API: a single read/write is capped at this many bytes (the cap
# is enforced on the UTF-8 encoded payload, not the character count).
MAX_FILE_BYTES = 5 * 2**20  # 5 MiB

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


def _resolve_host_path(rel: str, default_dir: Path) -> Path:
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
    loop, bad drive) raise ``ValueError`` -> the caller maps to ``bad_path``."""
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
        return p.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("bad_path") from exc


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
    return html(INDEX_HTML)


async def _help_corpus(request: Request):
    # The in-app Help guide's static cards, parsed from wiki/*.md (issue #60).
    # Public like "/" (help content is non-sensitive); built once at import in
    # help_corpus.py, so a wiki edit needs a broker restart to show up.
    return sanic_json(HELP_CORPUS)


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
    app.ctx.registry = BrokerRegistry()
    app.ctx.launcher = Launcher(
        app.ctx.registry,
        config.get("agent"),
        broker_port=port,
        token=app.ctx.auth_token,
    )
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
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return sanic_json({"ok": False, "error": "not_utf8"}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p),       # absolute, host-wide (#35)
                           "content": content})

    async def _file_write(request: Request):
        err = _file_auth_error(request)
        if err is not None:
            return err
        body = _json_object_body(request)
        if body is None:
            return sanic_json({"ok": False, "error": "bad_json"}, status=400)
        rel = body.get("path")
        content = body.get("content")
        if not isinstance(rel, str) or not rel:
            return sanic_json({"ok": False, "error": "bad_path"}, status=400)
        if not isinstance(content, str):
            return sanic_json({"ok": False, "error": "bad_content"},
                              status=400)
        data = content.encode("utf-8")
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
        # Destructive sibling of /file/write (#46): same host-wide resolution,
        # gate, and absolute-path echo. SINGLE FILE ONLY — is_file() refuses a
        # directory (and a symlink-to-dir, which is_file() reports False), so
        # this can never recurse a tree. Like read/write/upload, the path is
        # fully resolved first (_resolve_host_path ends in .resolve()), so a
        # symlink-to-file deletes its TARGET — the same link-following semantics
        # those endpoints already use. Used by the file manager's cross-pane
        # MOVE (copy-to-dest, then delete-source). No NEW privilege: /file/write
        # already grants full host-wide overwrite under this exact auth gate —
        # delete stays within it.
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
            os.unlink(str(p))
        except OSError as exc:
            return sanic_json({"ok": False, "error": str(exc)}, status=400)
        return sanic_json({"ok": True,
                           "path": str(p)})      # absolute, host-wide (#35)

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
                           "mods_enabled": app.ctx.mods_enabled})

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

    app.add_route(_index, "/", methods=["GET"])
    app.add_route(_help_corpus, "/help-corpus.json", methods=["GET"])
    app.add_websocket_route(_browser_ws, "/ws")
    app.add_websocket_route(_control_ws, "/control")
    app.add_websocket_route(_producer_ws, "/browserland")
    app.add_route(_sessions, "/sessions", methods=["GET"])
    app.add_route(_profiles, "/profiles", methods=["GET"])
    app.add_route(_launch, "/launch", methods=["POST"])
    app.add_route(_file_list, "/file/list", methods=["POST"])
    app.add_route(_file_read, "/file/read", methods=["POST"])
    app.add_route(_file_write, "/file/write", methods=["POST"])
    app.add_route(_file_upload, "/file/upload", methods=["POST"])
    app.add_route(_file_delete, "/file/delete", methods=["POST"])
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
    # Explicit preflights (route resolution precedes request middleware, so
    # an unrouted OPTIONS would 405 before any middleware could answer).
    # Explicit name= per registration — auto-derived names collide.
    for path, route_name in (("/sessions", "preflight_sessions"),
                             ("/profiles", "preflight_profiles"),
                             ("/launch", "preflight_launch"),
                             ("/file/list", "preflight_file_list"),
                             ("/file/read", "preflight_file_read"),
                             ("/file/write", "preflight_file_write"),
                             ("/file/upload", "preflight_file_upload"),
                             ("/file/delete", "preflight_file_delete"),
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
                             ("/mcp/config", "preflight_mcp_config")):
        app.add_route(_preflight, path, methods=["OPTIONS"], name=route_name)
    app.error_handler.add(NotFound, _handle_404)

    return app
