"""Entry point: ``python -m webterm.mcptool`` (and the ``browserland-mcp`` script).

Resolves the broker URL + MCP token + scope (flag > env > default), wires the
:class:`BrowserlandClient` config into the FastMCP server, and runs it over stdio.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

# The broker's scope grammar, from the dependency-free protocol module (this
# package must not import webterm.broker).
from ..protocol import SCOPE_RE

DEFAULT_URL = "http://127.0.0.1:4445"

# Env var names. BROWSERLAND_MCP_URL is distinct from the producer's
# BROWSERLAND_BROKER_URL (a ws://…/browserland URL) on purpose. WEB_TERMINAL_MCP_TOKEN
# mirrors the broker's own pin var so one secret can serve both sides.
URL_ENV = "BROWSERLAND_MCP_URL"
TOKEN_ENV = "BROWSERLAND_MCP_TOKEN"
TOKEN_ENV_ALT = "WEB_TERMINAL_MCP_TOKEN"
# Multi-host (#24): a JSON array of {name,url,token} host descriptors. When set
# (flag or env) it supersedes the single-host --broker-url/--token shorthand.
HOSTS_ENV = "BROWSERLAND_MCP_HOSTS"
# #232: the scope this server declares to every host (a --hosts entry's own
# "scope" overrides it for that host). Empty = unscoped.
SCOPE_ENV = "BROWSERLAND_MCP_SCOPE"
_SCOPE_RULE = ("1-64 characters of A-Z a-z 0-9 . _ -, starting with a letter "
               "or digit")


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


def _resolve_scope(args: argparse.Namespace) -> Optional[str]:
    """The process-wide scope: ``--scope``, whose default is
    ``$BROWSERLAND_MCP_SCOPE`` (so the flag beats the env), or None when it
    is empty. Raises :class:`ValueError` on a name outside the broker's
    grammar; the message names both sources, since the flag's default cannot
    say which one supplied the value."""
    value = args.scope
    if not value:
        return None
    if SCOPE_RE.fullmatch(value) is None:
        raise ValueError(f"--scope/${SCOPE_ENV} {value!r} is not a valid "
                         f"scope name ({_SCOPE_RULE})")
    return value


def _parse_hosts(raw: str, scope: Optional[str] = None) -> list:
    """Parse the ``--hosts`` / ``$BROWSERLAND_MCP_HOSTS`` JSON into an ordered
    list of ``(name, url, token, scope)`` host descriptors.

    An entry's optional ``"scope"`` (#232) is that host's declared scope; an
    empty or absent one inherits ``scope``, the process-wide value (None =
    unscoped).

    Raises :class:`ValueError` with a precise message on anything malformed: not
    a JSON array, an empty array, a non-object entry, a missing/empty field, a
    name containing ``':'`` (the namespaced-id separator), a duplicate name, or
    a ``"scope"`` that is not a string or not a valid scope name (the message
    names the host)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"--hosts/${HOSTS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError(
            f"--hosts/${HOSTS_ENV} must be a non-empty JSON array of "
            '{"name","url","token"} objects')
    hosts = []
    seen = set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"--hosts[{i}] is not a JSON object")
        name, url, token = item.get("name"), item.get("url"), item.get("token")
        for field, val in (("name", name), ("url", url), ("token", token)):
            if not isinstance(val, str) or not val:
                raise ValueError(
                    f"--hosts[{i}] '{field}' must be a non-empty string")
        if ":" in name:
            raise ValueError(
                f"--hosts[{i}] name {name!r} must not contain ':' "
                "(it is the namespaced-id separator)")
        if name in seen:
            raise ValueError(f"--hosts has a duplicate host name {name!r}")
        seen.add(name)
        host_scope = item.get("scope", "")
        if not isinstance(host_scope, str):
            raise ValueError(f"--hosts[{i}] ({name!r}) 'scope' must be a "
                             "string (empty or absent inherits --scope)")
        if host_scope and SCOPE_RE.fullmatch(host_scope) is None:
            raise ValueError(f"--hosts[{i}] ({name!r}) scope {host_scope!r} "
                             f"is not a valid scope name ({_SCOPE_RULE})")
        hosts.append((name, url, token, host_scope or scope))
    return hosts


def _resolve_hosts(args: argparse.Namespace) -> Optional[list]:
    """Resolve the host map as ``(name, url, token, scope)`` tuples. With
    ``--hosts``/env set, parse it (multi-host). Otherwise fall back to the
    single-host ``--broker-url``/``--token`` shorthand under the name
    ``"default"``. Every host inherits the process-wide ``--scope`` unless
    its own entry names one. Returns ``None`` when single-host mode has no
    resolvable token (so the caller can print the token help and exit)."""
    scope = _resolve_scope(args)
    if args.hosts:
        return _parse_hosts(args.hosts, scope)
    token = _resolve_token(args)
    if not token:
        return None
    return [("default", args.broker_url, token, scope)]


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
    p.add_argument(
        "--hosts", default=os.environ.get(HOSTS_ENV, ""),
        help='Multi-host: a JSON array of {"name","url","token"} descriptors, '
             "e.g. "
             '\'[{"name":"local","url":"http://127.0.0.1:4445","token":"…"}]\' '
             f"(default ${HOSTS_ENV}). When set, supersedes --broker-url/--token; "
             "window ids become namespaced '<host>:<int>'. An entry may add "
             '"scope" to declare its own scope for that host.',
    )
    p.add_argument(
        "--scope", default=os.environ.get(SCOPE_ENV, ""),
        help=f"Declare an MCP scope (default ${SCOPE_ENV}): this server then "
             "sees and drives only the windows tagged with it, and a host "
             "that does not echo it back on /mcp/info is refused. A --hosts "
             'entry\'s own "scope" overrides it for that host. Empty = '
             "unscoped (every window its access mode allows).",
    )
    return p.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)
    try:
        hosts = _resolve_hosts(args)
    except (OSError, ValueError) as exc:
        print(f"browserland-mcp: {exc}", file=sys.stderr)
        return 2
    if hosts is None:
        print(
            "browserland-mcp: no MCP token. Set --token, "
            f"${TOKEN_ENV} (or ${TOKEN_ENV_ALT}), --token-file pointing at a "
            f"webterm_mcp.json sidecar, or --hosts/${HOSTS_ENV} for multi-host.",
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

    server.configure(hosts)
    server.mcp.run()  # stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
