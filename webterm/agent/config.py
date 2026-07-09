"""Frozen agent configuration, resolved from CLI flags + environment."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Optional, Tuple

DEFAULT_BROKER_URL = "ws://127.0.0.1:4445/browserland"
BROKER_URL_ENV = "BROWSERLAND_BROKER_URL"
TOKEN_ENV = "WEB_TERMINAL_TOKEN"


def default_command() -> Tuple[str, ...]:
    if os.name == "nt":
        return (os.environ.get("COMSPEC", "cmd.exe"),)
    return ("bash", "-l")


def random_window_id() -> int:
    """48-bit random id. The registry keys on window_id alone and replaces
    on collision, so a low id could hijack an OS-native window-handle id
    (HWND/XID) — random 48-bit ids stay clear of both window handles and
    the broker-launcher range (>= 2**52), while remaining < 2**53 for
    exact representation in the picker's JS."""
    return secrets.randbits(48)


@dataclass(frozen=True)
class AgentConfig:
    command: Tuple[str, ...]
    broker_url: str = DEFAULT_BROKER_URL
    auth_token: Optional[str] = None
    cols: int = 80
    rows: int = 24
    title: Optional[str] = None
    window_id: int = field(default_factory=random_window_id)
    ring_bytes: int = 256 * 1024
    snapshot_mode: str = "raw"  # "raw" | "pyte"
    cwd: Optional[str] = None
    # #115: the launch-profile name this agent was spawned from, echoed in the
    # hello so the broker/UI can seed a per-profile default terminal color.
    profile: Optional[str] = None
    # Windows only: "auto" acquires a hidden console + re-enables Ctrl-C so
    # ConPTY's ^C works even when detached (#25), falling back to WinPTY if
    # acquisition fails; "winpty" forces the legacy backend.
    pty_backend: str = "auto"
