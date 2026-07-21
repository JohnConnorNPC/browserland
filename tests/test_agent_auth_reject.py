"""Agent behaviour when the broker refuses it for a missing token (#142).

Since a token is now required on /browserland too, an agent launched by an older
tokenless broker gets close code 4401 on every reconnect. That rejection arrives
POST-upgrade — the TCP connect and the WS handshake both succeeded — so the
backoff has already been reset by the time it lands, and the naive loop retries
at a steady ~1 Hz forever.

These pin the two halves of the fix: the agent backs off to the 10s cap instead
of hammering, and it says once, at ERROR, which environment variable to set.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from webterm.agent.client import _BACKOFF_CAP, BrokerClient


class _RejectingBroker:
    """Closes every /browserland upgrade with 4401, exactly like the broker's
    _producer_ws does for an unauthenticated producer, and counts the dials."""

    def __init__(self):
        self.attempts = 0
        self._server = None
        self.port = None

    async def start(self):
        self._server = await websockets.serve(
            self._handler, "127.0.0.1", 0, max_size=None)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws):
        self.attempts += 1
        await ws.close(code=4401, reason="auth required")

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/browserland"


class _State:
    window_id, pid, title, cols, rows = 42, 1, "t", 80, 24
    host, kind, agent, cwd, profile = "h", "agent", "", "/", "p"
    version, pyte = "test", False


def _client(url, token=None):
    return BrokerClient(
        url, token, _State(), asyncio.Queue(),
        on_input=lambda _b: None,
        on_resize=lambda _c, _r: None,
        on_snapshot_request=lambda: None,
    )


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_4401_backs_off_to_the_cap_instead_of_hammering(token):
    """Without the fix the agent redials every ~0.5s. With it, the first
    rejection pushes the delay straight to the 10s cap, so a window far shorter
    than that sees exactly one retry after the initial dial."""
    async def scenario():
        broker = _RejectingBroker()
        await broker.start()
        client = _client(broker.url, token)
        task = asyncio.create_task(client.run())
        try:
            # Comfortably longer than the un-fixed 0.5s backoff, far shorter
            # than the 10s cap the fix jumps to.
            await asyncio.sleep(2.0)
            assert broker.attempts <= 2, (
                f"agent redialled {broker.attempts}x in 2s - it is hammering a "
                f"broker that already refused it")
            assert broker.attempts >= 1
            assert client._auth_rejected is True
        finally:
            await client.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await broker.stop()

    asyncio.run(scenario())


def test_4401_logs_the_env_var_once(caplog):
    """A stranded terminal is otherwise silently dead. One ERROR naming
    $WEB_TERMINAL_TOKEN is what makes it legible — and only one, or the 10s
    retry loop turns the log into noise."""
    async def scenario():
        broker = _RejectingBroker()
        await broker.start()
        client = _client(broker.url)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.sleep(1.5)
        finally:
            await client.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await broker.stop()

    with caplog.at_level("ERROR", logger="webterm.agent.client"):
        asyncio.run(scenario())
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    message = errors[0].getMessage()
    assert "WEB_TERMINAL_TOKEN" in message
    assert "--print-token" in message


def test_the_latch_clears_once_the_token_is_accepted():
    """Rejection is deliberately NOT fatal: the token can come back without the
    agent restarting (an operator restoring a rolled-back webterm_token.json, or
    restarting the broker with the token this agent already holds). So a broker
    that refuses once and then accepts must end with a registered agent and a
    cleared latch — otherwise one auth blip pins a healthy agent at the 10s cap
    for the rest of its life."""
    class _RejectOnceThenAccept(_RejectingBroker):
        def __init__(self):
            super().__init__()
            self.hello = None
            self.registered = asyncio.Event()

        async def _handler(self, ws):
            self.attempts += 1
            if self.attempts == 1:
                await ws.close(code=4401, reason="auth required")
                return
            self.hello = await ws.recv()
            self.registered.set()
            # Drain until the client closes. An unresolvable await here would
            # hang websockets' wait_closed() at teardown.
            async for _ in ws:
                pass

    async def scenario():
        broker = _RejectOnceThenAccept()
        await broker.start()
        client = _client(broker.url, "tok")
        task = asyncio.create_task(client.run())
        try:
            # The retry is at the cap, so allow for it plus slack.
            await asyncio.wait_for(broker.registered.wait(), _BACKOFF_CAP + 5)
            assert broker.attempts == 2
            assert client.connected is True
            assert client._auth_rejected is False
        finally:
            await client.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await broker.stop()

    asyncio.run(scenario())
