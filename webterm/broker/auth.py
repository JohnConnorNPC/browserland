"""Token auth.

Policy (#142): **a token is required on every surface, on every interface,
always.** There is no loopback exemption and no opt-out. ``/browserland``
producers, ``/ws``, ``/control``, ``/sessions``, ``/profiles``, ``/launch``,
``/file/*``, ``/state`` and friends all refuse a request that carries no valid
token — 401 on HTTP, close code 4401 on a WebSocket.

Loopback used to be an exemption. It was never a sound one: the recommended
topology puts ``tailscale serve`` in front of a 127.0.0.1 bind, so every tailnet
request arrives *from* loopback; and a plain web page can dial
``ws://127.0.0.1:<port>/browserland`` (WebSockets are not CORS-gated) to
re-register a live window and inject fabricated terminal output.

Only ``GET /`` and ``GET /help-corpus.json`` stay public, plus the OPTIONS
preflights (which carry no credentials by design). The token is typed *into*
that page and auth is query/header-only with no cookies, so gating the document
itself would 401 every reload, bookmark and new tab forever. Neither response
carries host- or session-derived data.

A fresh install stays one-command usable: with nothing configured the broker
MINTS its own token into a sidecar (``webterm_token.json``) beside the state
store and prints a ready-to-open ``?token=`` URL.

Never log request URLs or args on auth failure — the token rides in the
query string.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Optional, Tuple

TOKEN_ENV = "WEB_TERMINAL_TOKEN"
MCP_TOKEN_ENV = "WEB_TERMINAL_MCP_TOKEN"

#: Sidecar holding the auto-minted browser token, a sibling of the state store
#: (``webterm_state.json``) like every other broker sidecar. Git-ignored.
AUTH_STATE_FILENAME = "webterm_token.json"

#: Bytes of entropy for a minted token, before urlsafe-base64 expansion.
_MINT_BYTES = 32


def _read_token_file(path) -> Optional[str]:
    """The token in ``path``, or None if it is missing/unreadable/malformed.

    Never raises: a corrupt sidecar degrades to "no token on disk" so the caller
    can re-mint rather than failing to boot."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        tok = data.get("auth_token")
        if isinstance(tok, str) and tok:
            return tok
    return None


def _write_token_file(path, token: str) -> bool:
    """Overwrite ``path`` with ``token``. True on success. Used only to repair a
    sidecar that exists but holds no usable token."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"auth_token": token}, fh)
    except OSError:
        return False
    return True


def resolve_existing_token(config: Optional[dict], path) -> Optional[str]:
    """The configured token WITHOUT minting one: env -> config -> sidecar.

    Env wins over the config file, so a unit file can override; the sidecar is
    the last resort (it only exists once a previous run minted). This is what
    ``--print-token`` uses — asking for the token must never be the thing that
    creates it."""
    env_tok = os.environ.get(TOKEN_ENV)
    if env_tok:
        return env_tok
    cfg_tok = (config or {}).get("auth_token")
    if isinstance(cfg_tok, str) and cfg_tok:
        return cfg_tok
    return _read_token_file(path)


def resolve_or_mint_token(config: Optional[dict], path) -> Tuple[str, str]:
    """``(token, source)`` — the live browser token. **Never None** (#142).

    ``source`` is one of:

    ``env``       $WEB_TERMINAL_TOKEN (a unit file / the parent broker)
    ``config``    broker_config's ``auth_token``
    ``file``      read back from the sidecar a previous run minted
    ``minted``    freshly generated and persisted THIS run
    ``ephemeral`` generated but NOT persisted (unwritable dir) — it changes on
                  every restart and ``--print-token`` cannot recover it, so the
                  caller must warn loudly

    The mint is ``O_CREAT|O_EXCL`` rather than an atomic replace: two brokers
    starting against the same state dir (the dev broker plus any test spawned
    with ``cwd=REPO``) both see no file and both mint, and a last-writer-wins
    replace would leave the loser running a token that is not the one on disk.
    With O_EXCL exactly one create succeeds and the other re-reads and adopts
    the winner, so both converge on one value.

    Mode 0o600 is honoured on POSIX (umask can only clear bits further). Windows
    has no POSIX mode: the file inherits the directory's ACL, so the state dir
    must itself be private — see docs/SETUP.md."""
    env_tok = os.environ.get(TOKEN_ENV)
    if env_tok:
        return env_tok, "env"
    cfg_tok = (config or {}).get("auth_token")
    if isinstance(cfg_tok, str) and cfg_tok:
        return cfg_tok, "config"

    path = Path(path)
    existing = _read_token_file(path)
    if existing:
        return existing, "file"

    minted = secrets.token_urlsafe(_MINT_BYTES)
    blob = json.dumps({"auth_token": minted})
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Someone created it between our read and our create. Whatever is on
        # disk wins so both brokers converge.
        adopted = _read_token_file(path)
        if adopted:
            return adopted, "file"
        # It exists but holds no usable token (truncated / hand-edited). A
        # tokenless sidecar protects nobody and would re-mint on every start,
        # so repair it in place, then re-read so a concurrent repair still
        # converges on a single value.
        if not _write_token_file(path, minted):
            return minted, "ephemeral"
        return (_read_token_file(path) or minted), "minted"
    except OSError:
        return minted, "ephemeral"

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(blob)
    except OSError:
        # Disk full mid-write: drop the half-file so the next start re-mints
        # cleanly instead of adopting a truncated one.
        try:
            os.unlink(str(path))
        except OSError:
            pass
        return minted, "ephemeral"
    return minted, "minted"


def resolve_mcp_token(config: Optional[dict]) -> Optional[str]:
    """Seed token for the MCP HTTP interface, mirroring resolve_token: env
    (``WEB_TERMINAL_MCP_TOKEN``) wins over ``config["mcp_token"]``. This is
    only the SEED — the live token may be overridden at runtime via the
    sidecar store (set/generated from the Control Panel). Distinct from the
    browser ``auth_token``: it gates the /mcp/* surface only."""
    return os.environ.get(MCP_TOKEN_ENV) or (config or {}).get("mcp_token") or None


def token_matches(provided: Optional[str], expected: Optional[str]) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8", "replace"),
        expected.encode("utf-8", "replace"),
    )


def provided_token(request) -> Optional[str]:
    """Token from ``?token=`` / ``?auth=`` or an Authorization: Bearer header."""
    tok = request.args.get("token") or request.args.get("auth")
    if tok:
        return tok
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def request_token_ok(request, expected: Optional[str]) -> bool:
    return token_matches(provided_token(request), expected)

# is_loopback_request() lived here until #142. It is gone rather than merely
# unused: as an auth input it was a loaded gun. It answered "did this connection
# arrive on a loopback listener", which is NOT "is this the same machine" — a
# request proxied by `tailscale serve` (the topology docs/SETUP.md recommends)
# arrives from loopback no matter which machine sent it, and any web page the
# user opens can reach loopback as well. Every gate now takes the token.
