"""PTY backends. Import the platform module lazily — linux_pty imports
Unix-only modules (pty/fcntl/termios), win_conpty imports pywinpty."""

from __future__ import annotations

import os

from .base import PtyBackend


def create_backend(pty_backend: str = "auto") -> PtyBackend:
    """pty_backend is Windows-only ("auto"|"conpty"|"winpty"); ignored on
    POSIX."""
    if os.name == "nt":
        from .win_conpty import WinConPtyBackend
        return WinConPtyBackend(backend=pty_backend)
    from .linux_pty import LinuxPtyBackend
    return LinuxPtyBackend()
