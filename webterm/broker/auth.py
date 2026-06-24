"""Token auth + loopback detection.

Policy (producer connections may be remote, unlike the deployed bridge):

* ``/browserland``: loopback connections are exempt (local terminals/agents
  work unchanged); non-loopback must present the token. No token configured
  -> non-loopback producers are refused.
* ``/launch``: token required whenever one is configured. With no token
  configured, only loopback requests are allowed — never an open RCE on a
  non-loopback bind.
* ``/ws`` + ``/sessions``: gated by the token only when one is configured.

Never log request URLs or args on auth failure — the token rides in the
query string.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Optional

TOKEN_ENV = "WEB_TERMINAL_TOKEN"
MCP_TOKEN_ENV = "WEB_TERMINAL_MCP_TOKEN"


def resolve_token(config: Optional[dict]) -> Optional[str]:
    """Env wins over the config file, so a unit file can override."""
    return os.environ.get(TOKEN_ENV) or (config or {}).get("auth_token") or None


def resolve_mcp_token(config: Optional[dict]) -> Optional[str]:
    """Seed token for the MCP HTTP interface, mirroring resolve_token: env
    (``WEB_TERMINAL_MCP_TOKEN``) wins over ``config["mcp_token"]``. This is
    only the SEED — the live token may be overridden at runtime via the
    sidecar store (set/generated from the Settings UI). Distinct from the
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


def is_loopback_request(request) -> bool:
    """True if the connection arrived on a loopback listener.

    Checks the transport's sockname (the local address the client dialed),
    same as the bridge's _is_loopback_sockname: a connection to the box's
    LAN IP is non-loopback even when it originates locally."""
    sockname = None
    transport = getattr(request, "transport", None)
    if transport is not None:
        try:
            sockname = transport.get_extra_info("sockname")
        except Exception:
            sockname = None
    if sockname:
        try:
            return ipaddress.ip_address(sockname[0]).is_loopback
        except (ValueError, IndexError):
            return False
    # Transport unavailable (some post-upgrade WS paths) — fall back to the
    # peer address; loopback peers can only exist on loopback listeners.
    try:
        return ipaddress.ip_address(request.ip).is_loopback
    except (TypeError, ValueError):
        return False
