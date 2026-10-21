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
from ..protocol import PEER_ID_RE, SCOPE_RE, normalize_origin, relayed_peer_id

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
# #245: the per-host MCP admin credential, sent as the
# X-Browserland-Mcp-Admin header IN ADDITION to the MCP token. Applies to the
# single-host --broker-url form; a --hosts entry admin credential is its own
# "admin_token" (no process-wide inheritance: an admin credential applies to
# one host only, by design).
ADMIN_TOKEN_ENV = "BROWSERLAND_MCP_ADMIN_TOKEN"
ADMIN_TOKEN_ENV_ALT = "WEB_TERMINAL_MCP_ADMIN_TOKEN"
_SCOPE_RULE = ("1-64 characters of A-Z a-z 0-9 . _ -, starting with a letter "
               "or digit")
# #261: the keys a hosts entry's nested peer object may carry; anything else
# is refused, so a typo ("tokn") is an error instead of a peer with no token.
_PEER_ENTRY_KEYS = ("token", "peer_origin", "scope", "admin_token")


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


def _resolve_admin_token(args: argparse.Namespace) -> Optional[str]:
    """The single-host admin credential (#245): ``--admin-token`` >
    ``$BROWSERLAND_MCP_ADMIN_TOKEN`` > ``$WEB_TERMINAL_MCP_ADMIN_TOKEN``;
    empty/unset = no admin credential (admin-only tools stay hidden). Unlike
    the scope there is NO process-wide default for ``--hosts`` entries: an
    admin credential applies to one host only."""
    if args.admin_token:
        return args.admin_token
    for env in (ADMIN_TOKEN_ENV, ADMIN_TOKEN_ENV_ALT):
        val = os.environ.get(env)
        if val:
            return val
    return None


def _parse_hosts(raw: str, scope: Optional[str] = None) -> list:
    """Parse the ``--hosts`` / ``$BROWSERLAND_MCP_HOSTS`` JSON into an ordered
    list of ``(name, url, token, scope, admin_token, peer_origin,
    discover_peers)`` host descriptors.

    An entry's optional ``"scope"`` (#232) is that host's declared scope; an
    empty or absent one inherits ``scope``, the process-wide value (None =
    unscoped). An entry's optional ``"admin_token"`` (#245) is that host's own
    admin credential; an empty or absent one means none (it never inherits a
    process-wide value — one admin credential, one host). An entry's
    optional ``"peer_origin"`` (#252) is, for a host reached through a hub's
    ``/peer/<id>`` prefix, the origin the hub is expected to dial for that
    peer; it is canonicalised exactly as the hub compares it, and the hub
    then refuses a re-pointed peer instead of receiving this token. A value
    that is not an origin -- surrounding whitespace included -- is refused
    here, never trimmed. Empty or absent means none.

    An entry's optional ``"peers"`` (#261) is an object of ``{id: {token,
    peer_origin?, scope?, admin_token?}}``: each peer becomes one more host
    right after its hub, named ``<hub>.<id>``, at ``<hub url>/peer/<id>``,
    with that peer's own token. Its scope is its own, else the process-wide
    ``scope`` -- never the hub entry's -- and its admin_token is its own
    only; its fields are checked as an entry's are, and any other key is
    refused. So is a ``<hub>.<id>`` that another entry is named, a peer
    token or admin_token equal to the hub entry's token or admin_token, and
    ``"peers"`` on an entry already behind ``/peer/<id>``.
    ``discover_peers`` is True on an entry that carries ``"peers"`` (``{}``
    included), so the server asks that hub which peers it relays.

    Raises :class:`ValueError` with a precise message on anything malformed:
    not a JSON array, a key repeated in one object, an empty array, a
    non-object entry, a missing/empty field, a name containing ``':'`` (the
    namespaced-id separator), a duplicate name, a ``"scope"`` that is not a
    string or not a valid scope name, an ``"admin_token"`` that is not a
    string, a ``"peer_origin"`` that is not a string or not an origin, or a
    ``"peers"`` that breaks the rules above (the message names the host, and
    the peer)."""
    try:
        data = json.loads(raw, object_pairs_hook=_no_repeated_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"--hosts/${HOSTS_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError(
            f"--hosts/${HOSTS_ENV} must be a non-empty JSON array of "
            '{"name","url","token"} objects')
    hosts = []
    seen = set()
    # #261: every explicit name, so a peer's `<hub>.<id>` is refused as a
    # collision whichever of the two comes first in the array.
    explicit = {}
    for i, item in enumerate(data):
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            explicit.setdefault(item["name"], i)
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
        where = f"--hosts[{i}] ({name!r})"
        host_scope, admin, peer_origin = _host_options(where, item)
        expanded = [(name, url, token, host_scope or scope, admin or None,
                     peer_origin or None, "peers" in item)]
        if "peers" in item:
            expanded += _peer_hosts(where, name, url, item["peers"], scope,
                                    (("token", token), ("admin_token", admin)))
            for host in expanded[1:]:
                if host[0] in explicit:
                    raise ValueError(
                        f"--hosts[{explicit[host[0]]}] name {host[0]!r} "
                        f"collides with peer {host[0][len(name) + 1:]!r} of "
                        f"host {name!r} (--hosts[{i}]): rename that entry, or "
                        "drop it or the peer")
        for host in expanded:
            if host[0] in seen:
                raise ValueError(
                    f"--hosts has a duplicate host name {host[0]!r}")
            seen.add(host[0])
        hosts.extend(expanded)
    return hosts


def _no_repeated_keys(pairs: list) -> dict:
    """``json.loads`` hook (#261 round 1): a key repeated in one object --
    a peer id given twice, two ``"token"`` keys -- is refused, where JSON
    would silently keep the last one."""
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(
                f"--hosts/${HOSTS_ENV} repeats the key {key!r} in one "
                "object (JSON would keep only the last one): remove the "
                "extra one")
        out[key] = value
    return out


def _host_options(where: str, item: dict) -> tuple:
    """The optional fields a hosts entry and a nested peer (#261) share:
    ``(scope, admin_token, peer_origin)``, each ``""`` when absent, the
    peer_origin canonicalised. Raises :class:`ValueError` naming ``where``."""
    host_scope = item.get("scope", "")
    if not isinstance(host_scope, str):
        raise ValueError(f"{where} 'scope' must be a string")
    if host_scope and SCOPE_RE.fullmatch(host_scope) is None:
        raise ValueError(f"{where} scope {host_scope!r} is not a valid scope "
                         f"name ({_SCOPE_RULE})")
    admin = item.get("admin_token", "")
    if not isinstance(admin, str):
        raise ValueError(f"{where} 'admin_token' must be a string")
    peer_origin = item.get("peer_origin", "")
    if not isinstance(peer_origin, str):
        raise ValueError(f"{where} 'peer_origin' must be a string")
    if peer_origin:
        origin, why = normalize_origin(peer_origin)
        if origin is None:
            raise ValueError(f"{where} peer_origin {peer_origin!r}: {why}")
        peer_origin = origin
    return host_scope, admin, peer_origin


def _peer_hosts(where: str, name: str, url: str, peers,
                scope: Optional[str], hub_credentials) -> list:
    """#261: a hub entry's ``"peers"`` as host descriptors, in config order:
    ``<name>.<id>`` at ``<url>/peer/<id>`` with the peer's own token. The
    peer's scope falls back to the process-wide ``scope``, never to the hub
    entry's; its admin_token never falls back. A peer token or admin_token
    equal to one of ``hub_credentials`` (the hub entry's ``(field, value)``
    pairs) is refused: the hub answers 403 hub_credential_refused to it
    anyway, so it would only spend a dial. So is ``"peers"`` on an entry that
    is already behind a hub's ``/peer/<id>``: the hub relays one hop only."""
    if not isinstance(peers, dict):
        raise ValueError(f"{where} 'peers' must be an object of "
                         "{id: {token, peer_origin, scope, admin_token}}")
    base = url.rstrip("/")
    if relayed_peer_id(base) is not None:
        raise ValueError(f"{where} is already behind a hub's /peer/ prefix, "
                         "so it cannot carry 'peers' (the hub relays one hop "
                         "only: peer_hop_refused)")
    out = []
    for pid, entry in peers.items():
        if PEER_ID_RE.fullmatch(pid) is None:
            raise ValueError(f"{where} peer id {pid!r} must be 1-64 "
                             "characters of A-Z a-z 0-9 _ -")
        pwhere = f"{where} peer {pid!r}"
        if not isinstance(entry, dict):
            raise ValueError(f"{pwhere} must be an object of "
                             f"{', '.join(_PEER_ENTRY_KEYS)}")
        unknown = sorted(k for k in entry if k not in _PEER_ENTRY_KEYS)
        if unknown:
            raise ValueError(f"{pwhere} has unknown key(s) {unknown}; "
                             f"allowed: {', '.join(_PEER_ENTRY_KEYS)}")
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError(f"{pwhere} 'token' must be a non-empty string")
        p_scope, p_admin, p_origin = _host_options(pwhere, entry)
        for field, value in (("token", token), ("admin_token", p_admin)):
            for hub_field, hub_value in hub_credentials:
                if value and value == hub_value:
                    raise ValueError(
                        f"{pwhere} '{field}' equals the hub entry's "
                        f"'{hub_field}': the hub never hands its own "
                        "credentials to a peer (hub_credential_refused); "
                        "give the peer its own MCP token")
        out.append((f"{name}.{pid}", f"{base}/peer/{pid}", token,
                    p_scope or scope, p_admin or None, p_origin or None,
                    False))
    return out


def _resolve_hosts(args: argparse.Namespace) -> Optional[list]:
    """Resolve the host map as ``(name, url, token, scope, admin_token,
    peer_origin, discover_peers)`` tuples. With ``--hosts``/env set, parse
    it (multi-host). Otherwise fall back to the single-host
    ``--broker-url``/``--token`` shorthand under the name ``"default"``,
    carrying ``--admin-token``/env as that host's admin credential (#245), no
    peer_origin and no peer discovery. Every host inherits the process-wide
    ``--scope`` unless its own entry names one. Returns ``None`` when
    single-host mode has no resolvable token (so the caller can print the
    token help and exit)."""
    scope = _resolve_scope(args)
    if args.hosts:
        return _parse_hosts(args.hosts, scope)
    token = _resolve_token(args)
    if not token:
        return None
    return [("default", args.broker_url, token, scope,
             _resolve_admin_token(args), None, False)]


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
             '"scope" to declare its own scope for that host, and "peers" '
             '({id: {"token": ...}}) to add that hub\'s peers as '
             "'<host>.<id>' hosts.",
    )
    p.add_argument(
        "--admin-token", default="",
        help=f"Per-host MCP admin token (#245), sent as the "
             "X-Browserland-Mcp-Admin header in addition to the MCP token "
             f"(overrides ${ADMIN_TOKEN_ENV} / ${ADMIN_TOKEN_ENV_ALT}). "
             "Applies to the single-host --broker-url form; a --hosts entry "
             "admin credential is its own \"admin_token\".",
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
