"""Minimal /browserland broker emulator for agent integration tests.

websockets-only (no Sanic): asserts hello-first, records every frame, and
lets tests script broker->producer traffic (input / resize /
snapshot_please) and kill the socket to exercise reconnect.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import websockets


class FakeBroker:
    def __init__(self) -> None:
        self.hellos: List[Dict[str, Any]] = []
        self.texts: List[Dict[str, Any]] = []
        self.binary: List[bytes] = []
        self.bad_first_frames: List[Any] = []
        self.producer = None
        self.connected = asyncio.Event()
        self._server = None
        self.port: Optional[int] = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/browserland"

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler, "127.0.0.1", 0, max_size=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, ws) -> None:
        first = await ws.recv()
        if isinstance(first, (bytes, bytearray)):
            self.bad_first_frames.append(first)
            return  # reference broker drops binary-before-hello
        hello = json.loads(first)
        if hello.get("type") != "hello":
            self.bad_first_frames.append(hello)
            return
        self.hellos.append(hello)
        self.producer = ws
        self.connected.set()
        try:
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    self.binary.append(bytes(msg))
                else:
                    self.texts.append(json.loads(msg))
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.producer is ws:
                self.producer = None
                self.connected.clear()

    # -- scripting helpers --------------------------------------------------

    async def send(self, obj: Dict[str, Any]) -> None:
        await self.producer.send(json.dumps(obj))

    async def send_input(self, data: str) -> None:
        await self.send({"type": "input", "data": data})

    async def send_resize(self, cols: int, rows: int) -> None:
        await self.send({"type": "resize", "cols": cols, "rows": rows})

    async def request_snapshot(self) -> None:
        await self.send({"type": "snapshot_please"})

    async def kill_producer(self) -> None:
        if self.producer is not None:
            await self.producer.close()

    async def wait_connected(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self.connected.wait(), timeout)

    async def wait_hello_count(self, n: int, timeout: float = 10.0) -> None:
        async def _poll() -> None:
            while len(self.hellos) < n:
                await asyncio.sleep(0.02)
        await asyncio.wait_for(_poll(), timeout)

    async def wait_binary(self, predicate, timeout: float = 5.0) -> bytes:
        """First binary frame matching predicate (also scans past frames)."""
        async def _poll() -> bytes:
            seen = 0
            while True:
                while seen < len(self.binary):
                    frame = self.binary[seen]
                    seen += 1
                    if predicate(frame):
                        return frame
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout)

    async def wait_text(self, predicate, timeout: float = 5.0) -> Dict[str, Any]:
        async def _poll() -> Dict[str, Any]:
            seen = 0
            while True:
                while seen < len(self.texts):
                    frame = self.texts[seen]
                    seen += 1
                    if predicate(frame):
                        return frame
                await asyncio.sleep(0.02)
        return await asyncio.wait_for(_poll(), timeout)
