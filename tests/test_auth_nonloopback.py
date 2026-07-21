"""Auth negatives on a non-loopback listener.

Binds a broker to 0.0.0.0 and dials the box's own LAN IP — the connection
then arrives on a non-loopback sockname, exactly like a remote client,
without needing a second machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import websockets

REPO = Path(__file__).resolve().parents[1]
TOKEN = "nonloopback-sekrit"


def _lan_ip():
    candidates = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            candidates.append(info[4][0])
    except socket.gaierror:
        pass
    # Egress-interface fallback: a UDP connect picks a routable source
    # address without sending a packet (TEST-NET-3 peer, never routed).
    # Covers boxes whose hostname doesn't resolve to a local address.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("203.0.113.1", 9))
            candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    for addr in candidates:
        if addr.startswith("127."):
            continue
        # Bind-verify: WSL propagates the Windows host's /etc/hosts, so the
        # hostname can resolve to an address (e.g. the host's Tailscale IP)
        # that doesn't exist in this network namespace — dialing it would be
        # ECONNREFUSED before any auth surface is reached.
        try:
            with socket.socket() as probe:
                probe.bind((addr, 0))
        except OSError:
            continue
        return addr
    return None


LAN_IP = _lan_ip()
pytestmark = pytest.mark.skipif(
    LAN_IP is None, reason="no non-loopback IPv4 address on this box")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _spawn_broker(port: int, token: str = TOKEN) -> subprocess.Popen:
    # The token is ALWAYS explicit: these brokers run with cwd=REPO, and one
    # with nothing configured would mint webterm_token.json into the real repo
    # root — a live secret dropped beside the source tree (#142).
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("BROWSERLAND_BROKER_URL", None)
    env["WEB_TERMINAL_TOKEN"] = token
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker",
         "--host", "0.0.0.0", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while True:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=5).read()
            return proc
        except OSError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"broker exited early: {proc.returncode}")
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("broker did not come up")
        time.sleep(0.2)


def _post(url: str, body: bytes = b"{}", headers: dict = None):
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _headers(method: str, url: str):
    """(status, case-insensitive headers) — for the CORS assertions."""
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers


def test_launch_requires_a_token_on_loopback_too():
    """#142: loopback is NOT an exemption any more.

    This used to assert the opposite — remote /launch 403
    `launch_disabled_no_token`, loopback /launch allowed unauthenticated. That
    policy is exactly the hole: `tailscale serve` in front of a 127.0.0.1 bind
    makes every tailnet request arrive from loopback, so "loopback-only" handed
    the whole tailnet an RCE. Both interfaces now 401, and the SAME 401 shape
    on both (the UI's login overlay keys off it)."""
    port = _free_port()
    proc = _spawn_broker(port, token=TOKEN)
    try:
        for host in (LAN_IP, "127.0.0.1"):
            status, payload = _post(f"http://{host}:{port}/launch")
            assert status == 401, host
            assert payload["error"] == "auth_required", host
        # With the token, loopback works again: an unknown profile 400s, which
        # proves we got past the gate rather than being refused by it.
        status, payload = _post(
            f"http://127.0.0.1:{port}/launch?token={TOKEN}",
            body=json.dumps({"profile": "nope"}).encode())
        assert status == 400
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_cors_on_nonloopback():
    """CORS positives over the LAN IP — the exact surface the multi-host
    UI uses for its cross-origin fetches against a remote broker. The 401
    must carry ACAO too, or the login probe reads as a fetch TypeError."""
    port = _free_port()
    proc = _spawn_broker(port, token=TOKEN)
    try:
        status, headers = _headers("GET", f"http://{LAN_IP}:{port}/sessions")
        assert status == 401
        assert headers.get("Access-Control-Allow-Origin") == "*"
        status, headers = _headers(
            "GET", f"http://{LAN_IP}:{port}/sessions?token={TOKEN}")
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == "*"
        status, headers = _headers(
            "OPTIONS", f"http://{LAN_IP}:{port}/launch")
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert (headers.get("Access-Control-Allow-Methods")
                == "GET, POST, PUT, OPTIONS")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_sessions_401s_over_the_lan_with_cors_intact():
    """#142: /sessions is no longer readable unauthenticated on ANY interface.

    This used to assert 200 for a tokenless broker over the LAN — an open read
    of every terminal's title, pid and cwd to anything that could route to the
    box. It is now 401, but the CORS header must survive the change: without
    ACAO on the 401 the UI's cross-origin login probe reads as a fetch
    TypeError and the amber auth chip never appears. The preflight stays 204
    and unauthenticated (it carries no credentials by design)."""
    port = _free_port()
    proc = _spawn_broker(port, token=TOKEN)
    try:
        status, headers = _headers("GET", f"http://{LAN_IP}:{port}/sessions")
        assert status == 401
        assert headers.get("Access-Control-Allow-Origin") == "*"
        status, headers = _headers("OPTIONS", f"http://{LAN_IP}:{port}/state")
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "*"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_producer_ws_requires_a_token_on_loopback_too():
    """#142: /browserland was exempt for loopback even WITH a token configured
    — the one gate the token never covered.

    WebSockets are not CORS-gated, so any web page the user had open could dial
    ws://127.0.0.1:<port>/browserland, re-register a LIVE window_id (kicking the
    real agent off with close 1012) and inject fabricated terminal output into a
    window the user trusts. A loopback producer now needs the token like
    everyone else."""
    port = _free_port()
    proc = _spawn_broker(port, token=TOKEN)
    try:
        async def scenario():
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/browserland", max_size=None)
            try:
                # Post-upgrade 4401, not an opaque 1006: the agent has to be
                # able to tell "wrong token" from "network died".
                with pytest.raises(websockets.ConnectionClosed):
                    await asyncio.wait_for(ws.recv(), 5)
                assert ws.close_code == 4401
            finally:
                await ws.close()

            # ...and the same dial WITH the token registers normally, so this
            # is a gate and not a broken endpoint.
            ws = await websockets.connect(
                f"ws://127.0.0.1:{port}/browserland?token={TOKEN}",
                max_size=None)
            try:
                await ws.send(json.dumps({
                    "type": "hello", "window_id": 555003, "pid": 1,
                    "title": "local", "cols": 80, "rows": 24,
                    "kind": "agent"}))
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/sessions?token={TOKEN}")
                for _ in range(50):
                    with urllib.request.urlopen(request, timeout=5) as r:
                        sessions = json.loads(r.read())
                    if any(s["id"] == 555003 for s in sessions):
                        break
                    await asyncio.sleep(0.1)
                assert any(s["id"] == 555003 for s in sessions)
            finally:
                await ws.close()

        asyncio.run(scenario())
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_browser_and_control_ws_require_a_token_on_loopback():
    """The other two WS surfaces, same policy: an unauthenticated /ws could
    attach to any PTY and /control could steal the single-active-browser lease
    out from under the real client."""
    port = _free_port()
    proc = _spawn_broker(port, token=TOKEN)
    try:
        async def scenario():
            for path in ("/ws?id=1", "/control?clientId=probe"):
                ws = await websockets.connect(
                    f"ws://127.0.0.1:{port}{path}", max_size=None)
                try:
                    with pytest.raises(websockets.ConnectionClosed):
                        await asyncio.wait_for(ws.recv(), 5)
                    assert ws.close_code == 4401, path
                finally:
                    await ws.close()

        asyncio.run(scenario())
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_producer_ws_token_gate_on_nonloopback():
    port = _free_port()
    proc = _spawn_broker(port, token=TOKEN)
    try:
        async def scenario():
            # Without token: post-upgrade close 4401 (not an opaque 1006).
            ws = await websockets.connect(
                f"ws://{LAN_IP}:{port}/browserland", max_size=None)
            try:
                with pytest.raises(websockets.ConnectionClosed):
                    await asyncio.wait_for(ws.recv(), 5)
                assert ws.close_code == 4401
            finally:
                await ws.close()

            # With ?token=: hello registers (the remote-agent path).
            ws = await websockets.connect(
                f"ws://{LAN_IP}:{port}/browserland?token={TOKEN}",
                max_size=None)
            try:
                await ws.send(json.dumps({
                    "type": "hello", "window_id": 555002, "pid": 1,
                    "title": "remote", "cols": 80, "rows": 24,
                    "kind": "agent"}))
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/sessions?token={TOKEN}")
                for _ in range(50):
                    with urllib.request.urlopen(request, timeout=5) as r:
                        sessions = json.loads(r.read())
                    if any(s["id"] == 555002 for s in sessions):
                        break
                    await asyncio.sleep(0.1)
                entry = next(s for s in sessions if s["id"] == 555002)
                assert entry["kind"] == "agent"
            finally:
                await ws.close()

        asyncio.run(scenario())
    finally:
        proc.terminate()
        proc.wait(timeout=5)
