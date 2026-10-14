"""A recording fake peer B for the #252 phase-2 socket tests, and a fake page.

:class:`FakeUpstreamWs` is a ``websockets`` server on 127.0.0.1:0 in its OWN
thread and loop (the ``peer_upstream.FakeUpstream`` pattern), so it outlives
whatever loop the code under test runs in. Every handshake is recorded as a
:class:`Handshake` (path, query, headers in arrival order with repeats kept),
every socket as a :class:`Conn` (the frames the relay sent it, in order and
typed, and the close frame it received). What a socket does is scripted by
its path (:data:`SCRIPTS`).

:class:`FakeDown` stands in for Sanic's server-side ``Websocket`` in the relay
core's tests and behaves as it does: ``recv`` returns ``None`` only on a
timeout, a close that lands mid-wait RAISES ``CancelledError``, a closed
socket raises ``WebsocketClosed``, and ``ws_proto`` carries the websockets
``State`` and the close frames each way.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import List, Optional, Tuple

from sanic.exceptions import WebsocketClosed
from websockets import Close, State
from websockets.asyncio.server import serve

RESIZED = json.dumps({"type": "resized", "cols": 80, "rows": 24,
                      "bootId": "fake-boot"})
BINARY = b"\x1b[31mhi\x00\xff"
STATUS = json.dumps({"type": "status", "active": False,
                     "activeClientId": None, "gen": 7,
                     "bootId": "fake-boot"})
#: A server close frame with no payload (opcode 8, length 0): websockets
#: reads it as code 1005, which no close frame may carry.
EMPTY_CLOSE = bytes([0x88, 0x00])
#: A server close frame with code 2000, which websockets' close() refuses.
CLOSE_2000 = bytes([0x88, 0x02]) + (2000).to_bytes(2, "big")


@dataclass
class Handshake:
    path: str
    query: str
    headers: List[Tuple[str, str]]

    def all(self, name: str) -> List[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    @property
    def names(self) -> set:
        return {k.lower() for k, _ in self.headers}


@dataclass
class Conn:
    path: str
    frames_in: list = field(default_factory=list)
    #: The close frame this socket RECEIVED from the relay: (code, reason),
    #: or (None, "") when the TCP connection died without one.
    closed: Optional[Tuple[Optional[int], str]] = None
    done: threading.Event = field(default_factory=threading.Event)
    ws: object = None


async def _ws(conn, ws):
    await ws.send(RESIZED)
    await ws.send(BINARY)


async def _control(conn, ws):
    await ws.send(STATUS)


async def _close_4409(conn, ws):
    await ws.close(4409, "deactivated")


async def _raw_close(ws, frame):
    # Straight to the transport, past websockets' own checks.
    ws.transport.write(frame)
    await asyncio.sleep(0.2)
    ws.transport.abort()


async def _close_4401(conn, ws):
    # B refusing its token after the upgrade, as a real broker does.
    await ws.close(4401, "auth required")


async def _close_2000(conn, ws):
    await _raw_close(ws, CLOSE_2000)


async def _close_empty(conn, ws):
    await _raw_close(ws, EMPTY_CLOSE)


async def _never(conn, ws):
    return None


async def _burst(conn, ws):
    for i in range(6):
        await ws.send("f%d" % i)


async def _tick(conn, ws):
    for i in range(5):
        await asyncio.sleep(0.3)
        await ws.send("t%d" % i)


#: path -> what the socket does once open. Every socket also records the
#: frames it receives and, on the echo paths, sends each one back.
SCRIPTS = {"/ws": _ws, "/control": _control, "/close-4409": _close_4409,
           "/close-4401": _close_4401,
           "/close-2000": _close_2000, "/close-empty": _close_empty,
           "/never": _never, "/burst": _burst, "/tick": _tick}
ECHO = {"/ws", "/control"}


class FakeUpstreamWs:
    def __init__(self) -> None:
        self.handshakes: List[Handshake] = []
        self.conns: List[Conn] = []
        #: When set, every handshake is answered 302 to this URL instead.
        self.redirect_to: Optional[str] = None
        self.port = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeUpstreamWs":
        self._thread.start()
        assert self._ready.wait(10), "fake upstream ws did not start"
        return self

    def stop(self) -> None:
        if self._loop is None or self._loop.is_closed():
            return

        async def shutdown():
            self._server.close()
            await self._server.wait_closed()
            self._loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        except RuntimeError:
            return
        self._thread.join(10)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close_all(self) -> None:
        """Drop every open socket WITHOUT a close frame (B was killed)."""
        def _abort():
            for c in self.conns:
                if c.ws is not None and c.ws.transport is not None:
                    c.ws.transport.abort()
        self._loop.call_soon_threadsafe(_abort)

    def wait_conn(self, n: int = 1, timeout: float = 5.0) -> Conn:
        """The n-th socket (1-based), once it exists."""
        pause = threading.Event()
        for _ in range(int(timeout / 0.02)):
            if len(self.conns) >= n:
                return self.conns[n - 1]
            pause.wait(0.02)
        raise AssertionError("no socket #%d at the fake" % n)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def boot():
            self._server = await serve(
                self._handle, "127.0.0.1", 0, max_size=None,
                compression=None, process_request=self._process_request)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        self._loop.run_until_complete(boot())
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    def _process_request(self, ws, request):
        path, _, query = request.path.partition("?")
        self.handshakes.append(Handshake(path, query,
                                         list(request.headers.raw_items())))
        if self.redirect_to is not None:
            resp = ws.respond(HTTPStatus.FOUND, "")
            resp.headers["Location"] = self.redirect_to
            return resp
        return None

    async def _handle(self, ws) -> None:
        path, _, query = ws.request.path.partition("?")
        conn = Conn(path, ws=ws)
        # Through A the path is always /ws or /control: ?fake=<name> picks
        # another script (it crosses like any other query parameter).
        pick = urllib.parse.parse_qs(query).get("fake")
        if pick:
            path = "/" + pick[0]
        self.conns.append(conn)

        async def reader():
            try:
                async for msg in ws:
                    conn.frames_in.append(msg)
                    if path in ECHO:
                        await ws.send(msg)
            except Exception:  # noqa: BLE001 -- the close is recorded below
                pass

        rd = asyncio.ensure_future(reader())
        try:
            script = SCRIPTS.get(path)
            if script is not None:
                await script(conn, ws)
            await ws.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        finally:
            rcvd = ws.protocol.close_rcvd
            conn.closed = (rcvd.code, rcvd.reason) if rcvd else (None, "")
            rd.cancel()
            conn.done.set()


class _Proto:
    def __init__(self) -> None:
        self.state = State.OPEN
        self.close_sent: Optional[Close] = None
        self.close_rcvd: Optional[Close] = None
        self.close_rcvd_then_sent: Optional[bool] = None


class FakeDown:
    """Sanic's server-side ``Websocket``, as far as the relay sees it."""

    def __init__(self) -> None:
        self.ws_proto = _Proto()
        self.sent: list = []
        self.close_code: Optional[int] = None
        self.close_reason: Optional[str] = None
        #: Cleared, every send waits on it (a page that stopped reading).
        self.drain = asyncio.Event()
        self.drain.set()
        #: When set, close() waits on it first (a slow close handshake).
        self.close_gate: Optional[asyncio.Event] = None
        self._incoming: asyncio.Queue = asyncio.Queue()
        self._cancel: Optional[asyncio.Future] = None

    def push(self, frame) -> None:
        """A frame from the page."""
        self._incoming.put_nowait(frame)

    def _closed(self) -> None:
        self.ws_proto.state = State.CLOSED
        if self._cancel is not None and not self._cancel.done():
            self._cancel.set_result(None)

    def page_close(self, code: int = 1000) -> None:
        """The page sends a close frame; Sanic answers it."""
        if self.ws_proto.state is not State.OPEN:
            return
        self.ws_proto.close_rcvd = Close(code, "")
        self.ws_proto.close_sent = Close(code, "")
        self.ws_proto.close_rcvd_then_sent = True
        self.close_code, self.close_reason = code, ""
        self._closed()

    def server_close(self, code: int = 1001) -> None:
        """Sanic's shutdown: the server sends a close first, the page
        answers it."""
        if self.ws_proto.state is not State.OPEN:
            return
        self.ws_proto.close_sent = Close(code, "")
        self.ws_proto.close_rcvd = Close(code, "")
        self.ws_proto.close_rcvd_then_sent = False
        self.close_code, self.close_reason = code, ""
        self._closed()

    async def recv(self, timeout: Optional[float] = None):
        if self.ws_proto.state is State.CLOSED:
            raise WebsocketClosed("closed")
        if not self._incoming.empty():
            return self._incoming.get_nowait()
        self._cancel = asyncio.get_running_loop().create_future()
        get = asyncio.ensure_future(self._incoming.get())
        try:
            done, _ = await asyncio.wait({self._cancel, get}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if get in done:
                return get.result()
            if self._cancel in done:
                # As Sanic: a close mid-wait cancels the receive.
                raise asyncio.CancelledError()
            return None
        finally:
            get.cancel()
            self._cancel = None

    async def send(self, frame) -> None:
        if self.ws_proto.state is not State.OPEN:
            raise WebsocketClosed("closed")
        await self.drain.wait()
        self.sent.append(frame)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.ws_proto.state is not State.OPEN:
            return
        self.ws_proto.close_sent = Close(code, reason)
        self.close_code, self.close_reason = code, reason
        self._closed()
