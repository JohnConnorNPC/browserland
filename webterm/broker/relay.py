"""Browser-WS relay: ``/ws?session=<id>`` <-> the producer entry.

Adapted from the relay half of xterm-py ``browser/websocket_handler.py``
(a separate codebase at https://github.com/JohnConnorNPC/xterm-py);
the legacy no-session local-PTY path is dropped — this broker only relays.

Attach order invariant (from the reference): send the authoritative
``resized`` BEFORE requesting the snapshot, so xterm.js sizes itself first
and the snapshot's lines land at the right widths instead of being
wrapped/cropped on arrival.
"""

from __future__ import annotations

import logging
from typing import Optional

from .. import protocol
from .registry import BrokerRegistry

LOGGER = logging.getLogger(__name__)


async def handle_browser_ws(request, ws, registry: BrokerRegistry,
                            ctx=None) -> None:
    raw_id = request.args.get("session")
    session_id: Optional[int] = None
    if raw_id:
        try:
            session_id = int(raw_id)
        except ValueError:
            LOGGER.warning("invalid ?session=%r", raw_id)

    if session_id is None:
        await ws.send(protocol.error_frame("missing_session", 0))
        return

    entry = registry.get(session_id)
    if entry is None:
        await ws.send(protocol.error_frame("unknown_session", session_id))
        return

    # The browser's stable single-active-lease id (see app.py /control). A
    # socket dialed without it can still watch the stream but its input is
    # dropped (see the backstop below). Absent ctx (defensive) -> no lease
    # tracked, so the backstop treats every socket as inactive.
    client_id = request.args.get("clientId", "").strip()

    entry.add_subscriber(ws, client_id)
    try:
        try:
            await ws.send(protocol.resized_frame(entry.cols, entry.rows))
        except Exception as exc:
            LOGGER.debug("send initial resized failed: %s", exc)

        # Fresh snapshot so this browser sees current state immediately.
        await entry.request_snapshot()

        # Browser -> broker -> producer.
        async for message in ws:
            # Single-active-client backstop: only the browser that currently
            # holds the lease may DRIVE this PTY. A fresh atomic read of the
            # live lease per message closes the race between the lease flip and
            # this socket's 4409 close actually landing — an id-less or
            # non-active socket watches but its input/resize is silently
            # dropped (mouse is already a no-op). Output (broadcast) is never
            # gated, so every viewer still sees the stream.
            active = bool(client_id) and (
                ctx is not None and ctx.active_client_id == client_id)
            if isinstance(message, (bytes, bytearray)):
                if not active:
                    continue
                # Bare binary from the browser = raw input bytes.
                text = bytes(message).decode("utf-8", errors="replace")
                await entry.send_to_producer(protocol.input_frame(text))
                continue
            data = protocol.parse(message)
            if data is None:
                LOGGER.debug("browser bad json: %r", message[:200])
                continue
            mtype = data.get("type")
            if mtype in ("input", "paste"):
                if not active:
                    continue
                await entry.send_to_producer(protocol.input_frame(
                    str(data.get("data", ""))))
            elif mtype == "resize":
                if not active:
                    continue
                try:
                    cols = int(data.get("cols", 80))
                    rows = int(data.get("rows", 24))
                except (TypeError, ValueError):
                    continue
                await entry.send_to_producer(protocol.resize_frame(cols, rows))
            elif mtype == "mouse":
                pass  # not forwarded in v1
            else:
                LOGGER.debug("browser unknown msg type: %r", mtype)
    finally:
        entry.remove_subscriber(ws)
