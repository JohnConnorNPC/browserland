"""Entry point: ``python -m webterm.mcptool`` (and the ``browserland-mcp`` script).

Resolves the broker URL + MCP token (flag > env > default), wires the
:class:`BrowserlandClient` config into the FastMCP server, and runs it over stdio.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

DEFAULT_URL = "http://127.0.0.1:4445"

# Env var names. BROWSERLAND_MCP_URL is distinct from the producer's
# BROWSERLAND_BROKER_URL (a ws://…/browserland URL) on purpose. WEB_TERMINAL_MCP_TOKEN
# mirrors the broker's own pin var so one secret can serve both sides.
URL_ENV = "BROWSERLAND_MCP_URL"
TOKEN_ENV = "BROWSERLAND_MCP_TOKEN"
TOKEN_ENV_ALT = "WEB_TERMINAL_MCP_TOKEN"


def _token_from_file(path: str) -> Optional[str]:
    """Read the ``token`` field from a ``webterm_mcp.json`` sidecar."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    token = data.get("token")
    if token is None:
        # The sidecar stores null when the broker pins its token via env — the
        # secret is off disk, so the file can't supply it.
        return None
    if not isinstance(token, str):
        raise ValueError(f"{path}: 'token' is not a string")
    return token


def _resolve_token(args: argparse.Namespace) -> Optional[str]:
    """Token precedence: --token > $BROWSERLAND_MCP_TOKEN > $WEB_TERMINAL_MCP_TOKEN
    > --token-file. Empty strings are treated as unset."""
    if args.token:
        return args.token
    for env in (TOKEN_ENV, TOKEN_ENV_ALT):
        val = os.environ.get(env)
        if val:
            return val
    if args.token_file:
        return _token_from_file(args.token_file)
    return None


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="webterm.mcptool",
        description="Browserland MCP server — exposes a broker's /mcp/* interface "
                    "as MCP tools over stdio.",
    )
    p.add_argument(
        "--broker-url", default=os.environ.get(URL_ENV, DEFAULT_URL),
        help=f"Broker base URL (default ${URL_ENV} or {DEFAULT_URL}).",
    )
    p.add_argument(
        "--token", default="",
        help=f"MCP token (overrides ${TOKEN_ENV} / ${TOKEN_ENV_ALT}).",
    )
    p.add_argument(
        "--token-file", default="",
        help="Path to a webterm_mcp.json sidecar; reads its 'token' field "
             "(used only when no flag/env token is set).",
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)
    try:
        token = _resolve_token(args)
    except (OSError, ValueError) as exc:
        print(f"browserland-mcp: cannot read token file: {exc}", file=sys.stderr)
        return 2
    if not token:
        print(
            "browserland-mcp: no MCP token. Set --token, "
            f"${TOKEN_ENV} (or ${TOKEN_ENV_ALT}), or --token-file "
            "pointing at a webterm_mcp.json sidecar.",
            file=sys.stderr,
        )
        return 2

    # Import the server lazily so a missing optional dependency (the `mcp` SDK
    # or its `httpx`) produces a clean message rather than a traceback at module
    # load. Name the actual missing module so a partial install or a genuine
    # import regression isn't misattributed to "the SDK".
    try:
        from . import server
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "a required dependency"
        print(
            f"browserland-mcp: cannot import the MCP server — {missing} is not "
            f"installed ({exc}). Install the extra with: "
            "pip install -e \".[mcp]\"",
            file=sys.stderr,
        )
        return 2

    server.configure(args.broker_url, token)
    server.mcp.run()  # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
