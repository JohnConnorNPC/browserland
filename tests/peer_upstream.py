"""A recording fake broker B for the #252 peer-proxy tests.

A raw ``asyncio.start_server`` on 127.0.0.1:0 in its OWN thread and loop, so
it is independent of whatever loop the code under test runs in (the in-process
Sanic test clients start and stop theirs per request). It speaks just enough
HTTP/1.1: one request per connection (every answer says ``Connection: close``),
``Content-Length`` or chunked request bodies, and scripted answers by path.

Every request is recorded as :class:`Seen` (method, raw target, headers in
arrival order with repeats kept, body), so a test can assert what crossed the
hop -- and what did not.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

CHUNK = 64 * 1024


@dataclass
class Seen:
    method: str
    target: str
    headers: List[Tuple[str, str]]
    body: bytes

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    @property
    def query(self) -> str:
        return self.target.split("?", 1)[1] if "?" in self.target else ""

    def all(self, name: str) -> List[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def one(self, name: str) -> Optional[str]:
        vals = self.all(name)
        assert len(vals) <= 1, (name, vals)
        return vals[0] if vals else None


@dataclass
class Answer:
    status: int = 200
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    #: When set, the body is written in CHUNK pieces and, after the first
    #: one, the writer waits on this event (max ``gate_timeout`` s) before
    #: the rest -- the relay must have delivered piece one downstream first.
    gate: Optional[threading.Event] = None
    gate_timeout: float = 10.0
    #: When set, nothing is written until this event is set (dial-cap tests).
    hold: Optional[threading.Event] = None


def json_answer(obj, status=200, extra=()) -> Answer:
    body = json.dumps(obj).encode()
    return Answer(status, [("Content-Type", "application/json")] + list(extra),
                  body)


class FakeUpstream:
    def __init__(self) -> None:
        self.seen: List[Seen] = []
        self.routes: Dict[str, Callable[[Seen], Answer]] = {}
        self.gate_timed_out = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.port = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "FakeUpstream":
        self._thread.start()
        assert self._ready.wait(10), "fake upstream did not start"
        return self

    def stop(self) -> None:
        """Idempotent: a test may stop it early (a peer that went down)."""
        if self._loop is None or self._loop.is_closed():
            return

        async def shutdown():
            self._server.close()
            await self._server.wait_closed()
            self._loop.stop()

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        except RuntimeError:        # closed between the check and the call
            return
        self._thread.join(10)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def hits(self, path: str) -> int:
        return sum(1 for s in self.seen if s.path == path)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def boot():
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        self._loop.run_until_complete(boot())
        try:
            self._loop.run_forever()
        finally:
            self._server.close()
            self._loop.close()

    # -- one request -------------------------------------------------------
    async def _handle(self, reader, writer) -> None:
        try:
            line = (await reader.readline()).decode("latin-1").rstrip("\r\n")
            if not line:
                return
            method, target, _ = line.split(" ", 2)
            headers: List[Tuple[str, str]] = []
            while True:
                raw = (await reader.readline()).decode("latin-1")
                raw = raw.rstrip("\r\n")
                if not raw:
                    break
                name, _, value = raw.partition(":")
                headers.append((name.strip(), value.strip()))
            low = {k.lower(): v for k, v in headers}
            body = b""
            if "content-length" in low:
                body = await reader.readexactly(int(low["content-length"]))
            elif low.get("transfer-encoding", "").lower() == "chunked":
                while True:
                    size = int((await reader.readline()).strip(), 16)
                    if size == 0:
                        await reader.readline()
                        break
                    body += await reader.readexactly(size)
                    await reader.readline()
            seen = Seen(method, target, headers, body)
            self.seen.append(seen)
            route = self.routes.get(seen.path)
            answer = route(seen) if route else json_answer(
                {"ok": True, "broker_id": "fake-b", "path": seen.path,
                 "method": method})
            await self._write(writer, answer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 -- already gone
                pass

    async def _write(self, writer, answer: Answer) -> None:
        loop = asyncio.get_running_loop()
        if answer.hold is not None:
            await loop.run_in_executor(None, answer.hold.wait, 30)
        head = [f"HTTP/1.1 {answer.status} X"]
        names = {k.lower() for k, _ in answer.headers}
        for k, v in answer.headers:
            head.append(f"{k}: {v}")
        if "content-length" not in names:
            head.append(f"Content-Length: {len(answer.body)}")
        head.append("Connection: close")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        if answer.gate is None:
            writer.write(answer.body)
            await writer.drain()
            return
        writer.write(answer.body[:CHUNK])
        await writer.drain()
        ok = await loop.run_in_executor(None, answer.gate.wait,
                                        answer.gate_timeout)
        if not ok:
            self.gate_timed_out = True
        for i in range(CHUNK, len(answer.body), CHUNK):
            writer.write(answer.body[i:i + CHUNK])
            await writer.drain()


class LiveApp:
    """A Sanic app served for real on 127.0.0.1:<free port> in its own thread
    and loop, for tests that need CONCURRENT requests (the dial cap) or a
    client that reads the body as it streams. The in-process test clients
    serve one request at a time and hand back the whole body."""

    def __init__(self, app) -> None:
        self.app = app
        self.port = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "LiveApp":
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self._thread.start()
        assert self._ready.wait(20), "live app did not start"
        if self._error is not None:
            raise self._error
        return self

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Sanic's touchup rewrites its own Http methods once per process; a
        # second live app in the same process would re-run it on the already
        # rewritten source and fail. It is an optimisation only.
        self.app.config.TOUCHUP = False

        async def boot():
            self._server = await self.app.create_server(
                host="127.0.0.1", port=self.port,
                return_asyncio_server=True)
            await self._server.startup()
            await self._server.before_start()
            await self._server.after_start()

        try:
            self._loop.run_until_complete(boot())
        except BaseException as exc:  # noqa: BLE001 -- reported by start()
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    def stop(self) -> None:
        if self._loop is None or self._server is None:
            return

        async def shutdown():
            await self._server.before_stop()
            self._server.close()
            await self._server.wait_closed()
            await self._server.after_stop()
            self._loop.stop()

        asyncio.run_coroutine_threadsafe(shutdown(), self._loop)
        self._thread.join(20)
