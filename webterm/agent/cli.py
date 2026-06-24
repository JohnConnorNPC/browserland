"""Command-line parsing for ``python -m webterm.agent``."""

from __future__ import annotations

import argparse
import os
from typing import List, Optional, Sequence

from .config import (
    AgentConfig,
    BROKER_URL_ENV,
    DEFAULT_BROKER_URL,
    TOKEN_ENV,
    default_command,
    random_window_id,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m webterm.agent",
        description="Headless PTY agent speaking Browserland's web-terminal "
                    "producer protocol.",
        epilog="Everything after `--` is the command to run in the PTY "
               "(default: bash -l on POSIX, %COMSPEC% on Windows).",
    )
    parser.add_argument(
        "--broker-url",
        default=None,
        help=f"broker /browserland WS URL (precedence: ${BROKER_URL_ENV} env > "
             f"this flag > {DEFAULT_BROKER_URL})",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help=f"producer auth token, appended as ?token= to the WS URL "
             f"(default: ${TOKEN_ENV} env; required only for non-loopback "
             f"brokers)",
    )
    parser.add_argument("--cols", type=int, default=80)
    parser.add_argument("--rows", type=int, default=24)
    parser.add_argument("--title", default=None,
                        help="initial title (default: the command name)")
    parser.add_argument("--window-id", type=int, default=None,
                        help="pin the session id (default: random 48-bit)")
    parser.add_argument("--ring-bytes", type=int, default=256 * 1024,
                        help="output ring buffer cap for snapshots")
    parser.add_argument("--snapshot-mode", choices=("raw", "pyte"),
                        default="raw")
    parser.add_argument("--cwd", default=None,
                        help="working directory for the child")
    parser.add_argument("--pty-backend", choices=("auto", "conpty", "winpty"),
                        default="auto",
                        help="Windows only: auto = ConPTY when a console "
                             "window exists, WinPTY headless (ConPTY drops "
                             "Ctrl-C without one); ignored on POSIX")
    parser.add_argument("command", nargs="*",
                        help="command to run in the PTY")
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> AgentConfig:
    if argv is None:
        import sys
        argv = sys.argv[1:]
    argv = list(argv)

    # Split at the first bare `--`: everything after it is the command,
    # verbatim, even if it looks like our own flags.
    command: List[str] = []
    if "--" in argv:
        idx = argv.index("--")
        command = argv[idx + 1:]
        argv = argv[:idx]

    ns = build_parser().parse_args(argv)

    if ns.command:
        if command:
            raise SystemExit(
                "error: command given both before and after `--`")
        command = list(ns.command)
    if not command:
        command = list(default_command())

    broker_url = (
        os.environ.get(BROKER_URL_ENV)
        or ns.broker_url
        or DEFAULT_BROKER_URL
    )
    auth_token = ns.auth_token or os.environ.get(TOKEN_ENV) or None

    if ns.cols < 1 or ns.rows < 1:
        raise SystemExit("error: --cols/--rows must be >= 1")

    return AgentConfig(
        command=tuple(command),
        broker_url=broker_url,
        auth_token=auth_token,
        cols=ns.cols,
        rows=ns.rows,
        title=ns.title if ns.title is not None else os.path.basename(command[0]),
        window_id=ns.window_id if ns.window_id is not None else random_window_id(),
        ring_bytes=max(4096, ns.ring_bytes),
        snapshot_mode=ns.snapshot_mode,
        cwd=ns.cwd,
        pty_backend=ns.pty_backend,
    )
