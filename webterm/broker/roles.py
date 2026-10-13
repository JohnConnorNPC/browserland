"""Broker roles (#253): Hub / Node, and whether this broker hosts terminals.

Two stored keys, one derived label:

* ``serve_ui`` (default true) -- the page axis (#87). ``false`` is a **node**:
  no desktop page, reached over the API by another Browserland. ``true`` is a
  **hub**: the broker whose page you open.
* ``terminals`` (default true) -- the launch axis. ``false`` refuses new
  launches (``/launch``, ``/mcp/launch``, profile edits); sessions that already
  exist and the ``/browserland`` producer socket are untouched.
* ``role`` is DERIVED from ``serve_ui`` for ``/info``, the boot log and the
  docs. A ``role`` key in the config is accepted as an alias and folded into
  ``serve_ui`` here, then dropped, so the two can never drift.

:func:`resolve` is the one fold. ``__main__`` runs it with the CLI flags before
anything else reads the config, and ``create_app`` runs it again (it is
idempotent) so a direct caller cannot build a combination the CLI refuses.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

ROLES = ("hub", "node")


def role_of(serve_ui: bool) -> str:
    """The derived label: no page -> ``node``, else ``hub``."""
    return "hub" if serve_ui else "node"


def resolve(config: Dict[str, Any], cli_role: Optional[str] = None,
            cli_headless: bool = False,
            cli_no_terminals: bool = False) -> Optional[str]:
    """Fold the role alias and the CLI flags into ``config`` in place.

    Returns ``None`` when the result is a broker that may start, else a
    one-line reason. Precedence follows ``--host``/``--port``: the CLI beats
    the config. ``--headless`` is the alias of ``--role node``, so passing it
    with ``--role hub`` is a contradiction, not a precedence question.
    """
    if "role" in config:
        role = config.pop("role")
        if role not in ROLES:
            return (f"error: unknown role {role!r} in config "
                    f"(expected 'hub' or 'node')")
        serve_ui = role == "hub"
        if "serve_ui" in config and bool(config["serve_ui"]) != serve_ui:
            return (f"error: config role {role!r} contradicts serve_ui: "
                    f"{str(bool(config['serve_ui'])).lower()}; "
                    f"keep one of them")
        config["serve_ui"] = serve_ui
    if cli_headless and cli_role == "hub":
        return ("error: --headless is the alias of --role node; "
                "it contradicts --role hub")
    if cli_headless or cli_role == "node":
        config["serve_ui"] = False
    elif cli_role == "hub":
        config["serve_ui"] = True
    if "terminals" in config and not isinstance(config["terminals"], bool):
        return (f"error: terminals must be true or false, "
                f"got {config['terminals']!r}")
    if cli_no_terminals:
        config["terminals"] = False
    if (not bool(config.get("serve_ui", True))
            and config.get("terminals", True) is False):
        return ("error: refusing to start: a node (serve_ui: false) with "
                "terminals: false has no terminals for another broker to "
                "use; set terminals: true, or serve the page (role: hub)")
    return None
