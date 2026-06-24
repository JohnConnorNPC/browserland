"""``python -m webterm.agent [opts] [--] command...``"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .agent import Agent
from .cli import parse_args


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WEBTERM_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = parse_args()

    if config.snapshot_mode == "pyte":
        from .snapshot import pyte_snap
        if not pyte_snap.available():
            print("error: --snapshot-mode pyte requires the 'pyte' package "
                  "(pip install pyte)", file=sys.stderr)
            return 2

    agent = Agent(config)
    try:
        return asyncio.run(agent.run())
    except KeyboardInterrupt:
        agent.backend.kill()
        return 130


if __name__ == "__main__":
    sys.exit(main())
