"""``python -m webterm.broker --host 127.0.0.1 --port 4445``"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .app import DEFAULT_PORT, create_app, load_config


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
    ns = parser.parse_args()

    config = load_config(ns.config)
    host = ns.host or config.get("host") or "127.0.0.1"
    port = ns.port or int(config.get("port") or DEFAULT_PORT)

    app = create_app(config, port=port)
    # single_process avoids Sanic's multi-worker spawn path, which on
    # Windows re-imports the module and can't find an app built in main();
    # it also dodges an os.killpg call that doesn't exist on Windows.
    app.run(host=host, port=port, single_process=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
