"""``python -m webterm.broker --host 127.0.0.1 --port 4445``"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import auth
from .app import DEFAULT_PORT, _open_url, create_app, load_config


def _print_token(config: dict, port: int) -> int:
    """``--print-token``: report the token WITHOUT starting a server and without
    minting one. Asking for the token must never be the thing that creates it —
    a mint here would hand out a value the broker isn't running.

    Resolution mirrors the broker's own: $WEB_TERMINAL_TOKEN, then
    broker_config's ``auth_token``, then the sidecar beside the state store."""
    state_path = Path(
        config.get("state_path") or (Path(os.getcwd()) / "webterm_state.json")
    ).resolve()
    path = Path(
        config.get("auth_state_path")
        or (state_path.parent / auth.AUTH_STATE_FILENAME)).resolve()
    token = auth.resolve_existing_token(config, path)
    if not token:
        print(
            f"no auth token yet: nothing in ${auth.TOKEN_ENV}, no auth_token "
            f"in broker_config, and no {path}.\n"
            "Start the broker once - it mints one - or set auth_token "
            "yourself.", file=sys.stderr)
        return 1
    print(token)
    print(_open_url(config, port, token))
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WEBTERM_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        prog="python -m webterm.broker",
        description="webterm broker: relays browser WSes to PTY agents / "
                    "terminal windows.",
    )
    parser.add_argument("--host", default=None,
                        help="bind address (default: config or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"bind port (default: config or {DEFAULT_PORT}; "
                             f"4444 was an earlier broker's port)")
    parser.add_argument("--config", default=None,
                        help="path to broker_config.json "
                             "(default: $WEB_TERMINAL_CONFIG, then repo root)")
    parser.add_argument("--headless", action="store_true",
                        help="serve the JSON/WS API only — no desktop page or "
                             "help corpus (overrides serve_ui in config)")
    parser.add_argument("--print-token", action="store_true",
                        help="print this broker's auth token and the URL to "
                             "open, then exit (never mints one)")
    ns = parser.parse_args()

    config = load_config(ns.config)
    host = ns.host or config.get("host") or "127.0.0.1"
    port = ns.port or int(config.get("port") or DEFAULT_PORT)
    # CLI wins over config, matching --host/--port precedence. Fold into the
    # config dict so create_app's single config.get("serve_ui") read is the one
    # source of truth. There's no --no-headless; to default headless use the key.
    if ns.headless:
        config["serve_ui"] = False
    # Same folding for the resolved host, so the startup banner and
    # --print-token can build the ready-to-open URL off the config alone.
    config["host"] = host

    if ns.print_token:
        return _print_token(config, port)

    app = create_app(config, port=port)
    # single_process avoids Sanic's multi-worker spawn path, which on
    # Windows re-imports the module and can't find an app built in main();
    # it also dodges an os.killpg call that doesn't exist on Windows.
    app.run(host=host, port=port, single_process=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
