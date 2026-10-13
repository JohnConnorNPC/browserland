"""Browser-WS relay: ``/ws?session=<id>`` <-> the producer entry.

Adapted from the relay half of xterm-py ``browser/websocket_handler.py``
(a separate codebase at https://github.com/JohnConnorNPC/xterm-py);
the legacy no-session local-PTY path is dropped — this broker only relays.

Attach order invariant (from the reference): send the authoritative
``resized`` BEFORE requesting the snapshot, so xterm.js sizes itself first
and the snapshot's lines land at the right widths instead of being
wrapped/cropped on arrival.

Terminal control (#238): any subscriber may claim the terminal with a
``control`` frame. While a controller holds it, ONLY that socket's input and
resize reach the producer (the lease holder's included are dropped); with no
controller the single-active-browser lease rule applies, unchanged. Output,
the snapshot request and mouse (a no-op) are never gated. A socket attaching
reads ``resized`` then the terminal's control view, then gets the snapshot.

#248: a frame dropped by the LEASE rule (not by a controller or a revoke) is
answered with ``{type:'error', reason:'not_active', session}``, at most once
per socket per lease generation, so the page re-checks its lease.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .. import protocol
from .registry import CONTROL_SEND_TIMEOUT, BrokerRegistry

LOGGER = logging.getLogger(__name__)

# A lease generation no socket has been hinted for yet (#248 A5).
_NEVER = object()


def _drive_refusal(entry, ws, client_id: str, ctx) -> Optional[str]:
    """Why ``ws`` may NOT send input/resize to ``entry``'s producer now, or
    None when it may: ``"revoked"``, ``"controller"`` (#238) or ``"lease"``
    (the single-active-browser lease). Read fresh per frame, and again under
    the send lock (send_to_producer_if)."""
    if ws in entry.revoked:
        # Lost the terminal (POST release / another claim): it watches, even
        # if its clientId holds the lease, until it claims again.
        return "revoked"
    ctl = entry.controller
    if ctl is not None:
        return None if ctl.ws is ws else "controller"
    # Single-active-client backstop: only the browser that currently holds
    # the lease may DRIVE this PTY. A fresh atomic read of the live lease per
    # message closes the race between the lease flip and this socket's 4409
    # close actually landing -- an id-less or non-active socket watches but
    # its input/resize is dropped (#248: with one not_active hint). Absent
    # ctx (defensive) -> no lease tracked, so every socket is inactive.
    if bool(client_id) and (
            ctx is not None and ctx.active_client_id == client_id):
        return None
    return "lease"


async def _control_op(registry: BrokerRegistry, entry, ws, data) -> None:
    """One ``control`` frame from a subscriber: claim or release. A refusal
    answers an error frame and leaves the socket open."""
    op = data.get("op")
    if op == "claim":
        err = registry.claim(entry, ws, data.get("label"))
    elif op == "release":
        err = registry.release_by_ws(entry, ws)
    else:
        err = "bad_op" if registry.is_live(entry) else "unknown_session"
    if err is not None:
        try:
            await ws.send(protocol.error_frame(err, entry.id))
        except Exception as exc:
            LOGGER.debug("send control error failed: %s", exc)


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
    # dropped (the lease backstop in _drive_refusal). Absent ctx (defensive)
    # -> no lease tracked, so the backstop treats every socket as inactive.
    client_id = request.args.get("clientId", "").strip()

    try:
        # Subscribe, then `resized`, then the control view, all inside the
        # terminal's publisher section: no control change can be broadcast to
        # this socket ahead of its `resized`, and every later one is newer
        # than the PUBLISHED view it is told here. The finally covers a
        # cancellation in here (remove_subscriber is idempotent).
        async with registry.control_section(entry.id):
            # Taking the section can wait, and in that wait the entry can be
            # replaced (its subscribers already swept with 1012) or exit. Re-
            # check, or this socket would join a dead entry and freeze.
            live = registry.is_live(entry)
            if live:
                entry.add_subscriber(ws, client_id)
                try:
                    await ws.send(protocol.resized_frame(entry.cols,
                                                         entry.rows))
                    await ws.send(protocol.control_frame(entry.published_view))
                except Exception as exc:
                    LOGGER.debug("send initial resized/control failed: %s",
                                 exc)
        if not live:
            # Exactly what an attach to an unknown session gets.
            await ws.send(protocol.error_frame("unknown_session", session_id))
            return

        # Fresh snapshot so this browser sees current state immediately.
        await entry.request_snapshot()

        # #248 A5: the refusal reason and the lease generation are taken in
        # the SAME synchronous step as each gate decision (on arrival, and
        # again under the send lock), so a hint never describes a different
        # moment than the drop it reports.
        last = {"reason": None, "gen": None}
        hinted = {"gen": _NEVER}

        def may() -> bool:
            last["reason"] = _drive_refusal(entry, ws, client_id, ctx)
            last["gen"] = getattr(ctx, "lease_generation", None)
            return last["reason"] is None

        async def refused() -> None:
            # Advisory only: a frame dropped because this socket's client
            # does not hold the lease is answered with ONE error per lease
            # generation, so the page can re-check its lease instead of
            # typing into nothing. Never for a controller-gated or revoked
            # socket: the #239 controlled view already tells that page.
            # Bounded, so a stalled socket cannot wedge this read loop; the
            # generation counts as hinted only once a hint was delivered.
            if last["reason"] != "lease" or hinted["gen"] == last["gen"]:
                return
            gen = last["gen"]
            try:
                await asyncio.wait_for(
                    ws.send(protocol.not_active_frame(entry.id)),
                    CONTROL_SEND_TIMEOUT)
            except Exception as exc:
                LOGGER.debug("send not_active failed: %r", exc)
                return
            hinted["gen"] = gen

        # Browser -> broker -> producer. Every input/resize frame passes the
        # gate (_drive_refusal) on arrival AND again under the send lock: a
        # frame admitted just before a claim or revoke must not land after it.
        # Output (broadcast) is never gated, so every viewer sees the stream.
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                if not may():
                    await refused()
                    continue
                # Bare binary from the browser = raw input bytes.
                text = bytes(message).decode("utf-8", errors="replace")
                if not await entry.send_to_producer_if(
                        protocol.input_frame(text), may):
                    await refused()
                continue
            data = protocol.parse(message)
            if data is None:
                LOGGER.debug("browser bad json: %r", message[:200])
                continue
            mtype = data.get("type")
            # 'paste' is a legacy alias for 'input' (#138): kept for tabs
            # served before the UI switched to xterm's paste(); pinned by
            # test_broker_e2e.py::test_large_frame_relay.
            if mtype in ("input", "paste"):
                if not may():
                    await refused()
                    continue
                if not await entry.send_to_producer_if(protocol.input_frame(
                        str(data.get("data", ""))), may):
                    await refused()
            elif mtype == "resize":
                if not may():
                    await refused()
                    continue
                try:
                    cols = int(data.get("cols", 80))
                    rows = int(data.get("rows", 24))
                except (TypeError, ValueError):
                    continue
                if not await entry.send_to_producer_if(
                        protocol.resize_frame(cols, rows), may):
                    await refused()
            elif mtype == "control":
                await _control_op(registry, entry, ws, data)
            elif mtype == "mouse":
                pass  # not forwarded in v1
            else:
                LOGGER.debug("browser unknown msg type: %r", mtype)
    finally:
        entry.remove_subscriber(ws)
        # The socket is the lease: a controller that goes away releases. A
        # no-op for any other socket, or once the entry is dying (that path
        # force-released it already).
        registry.release_by_ws(entry, ws)
