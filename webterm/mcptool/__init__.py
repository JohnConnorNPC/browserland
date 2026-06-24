"""Browserland MCP server: a thin MCP (stdio) wrapper over a Browserland broker's
``/mcp/*`` HTTP interface. See README.md in this directory.

Re-exports are **lazy** (PEP 562): importing this package has no side effects and
pulls in neither ``httpx`` nor the optional ``mcp`` SDK until a name is actually
accessed. That keeps ``python -m webterm.mcptool`` reaching ``main()``'s clean
"dependency not installed" message instead of failing at package import.
"""

from __future__ import annotations

from typing import Any

__all__ = ["BrowserlandClient", "BrowserlandError", "main"]


def __getattr__(name: str) -> Any:
    if name in ("BrowserlandClient", "BrowserlandError"):
        from . import client
        return getattr(client, name)
    if name == "main":
        from .__main__ import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
