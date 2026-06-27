"""Broker end-to-end (the scriptable part of the M4 smoke).

Runs ``python -m webterm.broker`` as a subprocess with a token configured,
then exercises:
  * auth: /sessions 401 without token, /launch 401 without token,
    400 unknown profile, producer-protocol registration
  * relay: fake producer registers; browser attach gets resized-before-
    snapshot ordering; input/resize round-trip; binary passthrough
  * /launch with a real profile -> detached agent registers (id >= 2**52,
    kind == "agent", pid > 0), terminal round-trips input
  * /profiles: 401 without token, names-only shape with the default included
  * title broadcast: a producer 'title' frame reaches attached browsers live
    and /sessions reflects it
  * re-register: a second hello for the same id closes the old entry's
    browsers (1012) and a fresh attach reaches the new producer
  * GET / serves the windowed desktop page (multi-host markers included)
  * CORS: with a token configured, ACAO * on success AND error responses
    (401/404/405 — the cross-origin login probe depends on it), explicit
    OPTIONS preflights on /sessions, /profiles, /launch; withOUT a token,
    no CORS headers at all (tokenless loopback broker stays unreadable to
    arbitrary websites); sanic-ext stays neutralized (/docs 404)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
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
TOKEN = "test-sekrit-token"
LAUNCH_PROFILE = "cmd" if os.name == "nt" else "bash"
# Stable browser id for the single-active-client lease: a /ws socket only
# forwards input when its clientId holds the broker's active lease, so the
# input-driving tests claim it via /control first. Reused across tests so a
# reconnect with the same id stays active even if a prior release is in flight.
CLIENT_ID = "e2e-client"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(method: str, url: str, body: bytes = None, headers: dict = None):
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _request(method: str, url: str, body: bytes = None, headers: dict = None):
    """Like _http but returns (status, response headers, raw body) — the
    headers object is case-insensitive (email.message.Message), for the
    CORS assertions."""
    request = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


@pytest.fixture(scope="module")
def broker_proc():
    port = _free_port()
    env = dict(os.environ)
    env["WEB_TERMINAL_TOKEN"] = TOKEN
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("BROWSERLAND_BROKER_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while True:
        try:
            status, _ = _http("GET", f"{base}/sessions?token={TOKEN}")
            if status == 200:
                break
        except OSError:
            pass
        if proc.poll() is not None:
            raise RuntimeError(f"broker exited early: {proc.returncode}")
        if time.time() > deadline:
            proc.kill()
            raise RuntimeError("broker did not come up")
        time.sleep(0.2)
    yield proc, port, base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_auth_gates(broker_proc):
    _, _, base = broker_proc
    status, _ = _http("GET", f"{base}/sessions")
    assert status == 401
    status, payload = _http("POST", f"{base}/launch", body=b"{}")
    assert status == 401
    status, payload = _http(
        "POST", f"{base}/launch",
        body=json.dumps({"profile": "no-such-profile"}).encode(),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    assert status == 400
    assert payload["error"] == "unknown_profile"


async def _attach_browser(port: int, session_id: int,
                          client_id: str = CLIENT_ID):
    suffix = f"&clientId={client_id}" if client_id else ""
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/ws?session={session_id}&token={TOKEN}{suffix}",
        max_size=None)
    return ws


async def _claim_lease(port: int, client_id: str = CLIENT_ID):
    """Open a /control socket for ``client_id`` and read its first status
    frame. The first connection on a fresh broker auto-activates; a reconnect
    with the same id stays active. Returns (ws, status_dict)."""
    ws = await websockets.connect(
        f"ws://127.0.0.1:{port}/control?clientId={client_id}&token={TOKEN}")
    status = json.loads(await asyncio.wait_for(ws.recv(), 5))
    return ws, status


async def _next_input(ws, timeout: float = 5):
    """Read the producer's next frame, skipping snapshot_please (sent on every
    browser attach), and return the first input/resize frame."""
    while True:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if frame.get("type") == "snapshot_please":
            continue
        return frame


# NOTE: runs before the input-driving tests so the in-memory lease is pristine
# (None) when client A connects and auto-claims. Only /control connections
# touch the lease, so the earlier read-only tests leave it untouched.
def test_control_lease(broker_proc):
    """Single-active-browser lease end-to-end over raw WS: auto-activate the
    first /control, deactivate the rest, become_active hand-off (incl. the 4409
    terminal teardown), input gating on /ws, the /state not_active gate, and
    the release-on-disconnect-with-no-auto-promote + reconnect-reclaim path."""
    _, port, base = broker_proc

    async def scenario():
        # A connects first -> auto-activated; B connects -> inactive.
        ctrl_a, sa = await _claim_lease(port, "A")
        assert sa == {"type": "status", "active": True, "activeClientId": "A"}
        ctrl_b, sb = await _claim_lease(port, "B")
        assert sb == {"type": "status", "active": False, "activeClientId": "A"}

        # A live producer to gate input against.
        producer = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await producer.send(json.dumps({
            "type": "hello", "window_id": 555900, "pid": 5,
            "title": "lease", "cols": 80, "rows": 24}))
        for _ in range(50):
            _, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
            if any(s["id"] == 555900 for s in sessions):
                break
            await asyncio.sleep(0.1)

        # Active client A's terminal forwards input.
        br_a = await _attach_browser(port, 555900, "A")
        assert json.loads(await asyncio.wait_for(br_a.recv(), 5))["type"] \
            == "resized"
        await br_a.send(json.dumps({"type": "input", "data": "A1"}))
        assert await _next_input(producer) == {"type": "input", "data": "A1"}

        # B takes over: A is told it's inactive, B that it's active, and A's
        # already-attached terminal is closed with 4409 (deactivated).
        await ctrl_b.send(json.dumps({"type": "become_active"}))
        assert json.loads(await asyncio.wait_for(ctrl_a.recv(), 5)) == \
            {"type": "status", "active": False, "activeClientId": "B"}
        assert json.loads(await asyncio.wait_for(ctrl_b.recv(), 5)) == \
            {"type": "status", "active": True, "activeClientId": "B"}
        with pytest.raises(websockets.ConnectionClosed):
            while True:
                await asyncio.wait_for(br_a.recv(), 10)
        assert br_a.close_code == 4409

        # New active client B's terminal forwards input...
        br_b = await _attach_browser(port, 555900, "B")
        assert json.loads(await asyncio.wait_for(br_b.recv(), 5))["type"] \
            == "resized"
        await br_b.send(json.dumps({"type": "input", "data": "B1"}))
        assert await _next_input(producer) == {"type": "input", "data": "B1"}

        # ...but a freshly-attached NON-active (A) socket is input-inert: its
        # input never reaches the producer; B's next input arrives instead.
        br_a2 = await _attach_browser(port, 555900, "A")
        assert json.loads(await asyncio.wait_for(br_a2.recv(), 5))["type"] \
            == "resized"
        await br_a2.send(json.dumps({"type": "input", "data": "A2-dropped"}))
        await br_b.send(json.dumps({"type": "input", "data": "B2"}))
        assert await _next_input(producer) == {"type": "input", "data": "B2"}

        # /state PUT lease gate: non-active (A or id-less) -> 409 not_active;
        # the active client (B) writes fine.
        auth = {"Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json"}
        _, st = _http("GET", f"{base}/state?token={TOKEN}")
        rev = st["rev"]
        status, payload = _http(
            "PUT", f"{base}/state",
            body=json.dumps({"baseRev": rev, "settings": {}, "layout": {},
                             "clientId": "A"}).encode(), headers=auth)
        assert status == 409 and payload["error"] == "not_active"
        status, payload = _http(
            "PUT", f"{base}/state",
            body=json.dumps({"baseRev": rev, "settings": {}, "layout": {}}
                            ).encode(), headers=auth)
        assert status == 409 and payload["error"] == "not_active"
        status, payload = _http(
            "PUT", f"{base}/state",
            body=json.dumps({"baseRev": rev, "settings": {}, "layout": {},
                             "clientId": "B"}).encode(), headers=auth)
        assert status == 200 and payload["ok"] is True

        # B disconnects -> lease released (no auto-promote): the still-open A
        # control socket is told nobody is active.
        await ctrl_b.close()
        assert json.loads(await asyncio.wait_for(ctrl_a.recv(), 5)) == \
            {"type": "status", "active": False, "activeClientId": None}

        # A fresh client C reconnecting onto the empty lease auto-claims it.
        ctrl_c, sc = await _claim_lease(port, "C")
        assert sc == {"type": "status", "active": True, "activeClientId": "C"}

        await ctrl_a.close()
        await ctrl_c.close()
        await br_b.close()
        await br_a2.close()
        await producer.close()

    asyncio.run(scenario())


def test_fake_producer_relay(broker_proc):
    _, port, base = broker_proc

    async def scenario():
        # Loopback producer: no token needed (exemption under test).
        producer = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await producer.send(json.dumps({
            "type": "hello", "window_id": 555001, "pid": 99,
            "title": "fake", "cols": 80, "rows": 24}))
        # Hello has no ack; poll /sessions until it lands.
        for _ in range(50):
            status, sessions = _http(
                "GET", f"{base}/sessions?token={TOKEN}")
            if any(s["id"] == 555001 for s in sessions):
                break
            await asyncio.sleep(0.1)
        entry = next(s for s in sessions if s["id"] == 555001)
        assert entry["kind"] == "terminal"  # no kind in hello -> default
        assert entry["title"] == "fake"
        assert entry["pid"] == 99

        # Claim the single-active lease so this browser's input is forwarded.
        control, status = await _claim_lease(port)
        assert status["active"] is True
        browser = await _attach_browser(port, 555001)
        try:
            # Invariant: resized arrives BEFORE the snapshot is requested.
            first = json.loads(await asyncio.wait_for(browser.recv(), 5))
            assert first == {"type": "resized", "cols": 80, "rows": 24}
            req = json.loads(await asyncio.wait_for(producer.recv(), 5))
            assert req == {"type": "snapshot_please"}

            # Producer binary -> browser verbatim.
            await producer.send(b"\x1b[0m\x1b[2J\x1b[Hsnapshot-bytes")
            frame = await asyncio.wait_for(browser.recv(), 5)
            assert frame == b"\x1b[0m\x1b[2J\x1b[Hsnapshot-bytes"

            # Browser input/resize -> producer.
            await browser.send(json.dumps({"type": "input", "data": "ls\r"}))
            frame = json.loads(await asyncio.wait_for(producer.recv(), 5))
            assert frame == {"type": "input", "data": "ls\r"}
            await browser.send(json.dumps(
                {"type": "resize", "cols": 132, "rows": 43}))
            frame = json.loads(await asyncio.wait_for(producer.recv(), 5))
            assert frame == {"type": "resize", "cols": 132, "rows": 43}

            # Producer resized -> rebroadcast to the browser.
            await producer.send(json.dumps(
                {"type": "resized", "cols": 132, "rows": 43}))
            frame = json.loads(await asyncio.wait_for(browser.recv(), 5))
            assert frame == {"type": "resized", "cols": 132, "rows": 43}
        finally:
            await browser.close()
            await producer.close()
            await control.close()

        # Unknown session -> error frame.
        browser = await _attach_browser(port, 12345678)
        try:
            frame = json.loads(await asyncio.wait_for(browser.recv(), 5))
            assert frame["type"] == "error"
            assert frame["reason"] == "unknown_session"
            assert frame["session_id"] == 12345678
        finally:
            await browser.close()

    asyncio.run(scenario())


def test_large_frame_relay(broker_proc):
    """Frames past Sanic's default 1 MiB WEBSOCKET_MAX_SIZE used to get the
    socket killed with a 1009 close and the bytes silently vanished (Linux
    verification finding F2): a big paste on /ws, an oversized snapshot on
    /browserland. create_app now raises the cap to 16 MiB."""
    _, port, base = broker_proc

    async def scenario():
        producer = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await producer.send(json.dumps({
            "type": "hello", "window_id": 555004, "pid": 7,
            "title": "big", "cols": 80, "rows": 24}))
        for _ in range(50):
            status, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
            if any(s["id"] == 555004 for s in sessions):
                break
            await asyncio.sleep(0.1)

        control, status = await _claim_lease(port)
        assert status["active"] is True
        browser = await _attach_browser(port, 555004)
        try:
            first = json.loads(await asyncio.wait_for(browser.recv(), 5))
            assert first["type"] == "resized"
            req = json.loads(await asyncio.wait_for(producer.recv(), 5))
            assert req == {"type": "snapshot_please"}

            # ~2 MiB paste through /ws -> re-framed as input to the producer.
            paste = "p" * (2 * 1024 * 1024)
            await browser.send(json.dumps({"type": "paste", "data": paste}))
            frame = json.loads(await asyncio.wait_for(producer.recv(), 15))
            assert frame["type"] == "input"
            assert frame["data"] == paste

            # ~2 MiB binary (a snapshot from a big --ring-bytes) through
            # /browserland -> browser verbatim.
            blob = b"\x1b[0m\x1b[2J\x1b[H" + b"s" * (2 * 1024 * 1024)
            await producer.send(blob)
            echoed = await asyncio.wait_for(browser.recv(), 15)
            assert echoed == blob
        finally:
            await browser.close()
            await producer.close()
            await control.close()

    asyncio.run(scenario())


def test_index_serves_windowed_page(broker_proc):
    _, _, base = broker_proc
    with urllib.request.urlopen(f"{base}/", timeout=10) as response:
        assert response.status == 200
        body = response.read().decode("utf-8")
    assert "<title>Browserland</title>" in body   # product wordmark (todo2 13)
    assert 'rel="icon"' in body               # favicon shipped (todo2 14)
    assert "term-window" in body          # the windowed desktop shipped
    assert "btn-launch" in body
    # Multi-host build markers: hosts model, per-host URL builders, status
    # chips — and the old persist-token-in-the-URL block must be gone
    # (tokens live in localStorage now).
    assert "_hosts" in body
    assert "hostHttpUrl" in body
    assert "host-status" in body
    assert "searchParams.set('token'" not in body


def test_cors_with_token(broker_proc):
    """With a token configured, ACAO * must ride on success AND on the
    401: without it a cross-origin login probe surfaces as a fetch
    TypeError and the UI can't tell "wrong password" from "host down"."""
    _, _, base = broker_proc
    status, headers, _ = _request("GET", f"{base}/sessions?token={TOKEN}")
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == "*"
    status, headers, _ = _request("GET", f"{base}/sessions")
    assert status == 401
    assert headers.get("Access-Control-Allow-Origin") == "*"
    status, headers, _ = _request("GET", f"{base}/")
    assert status == 200
    assert headers.get("Access-Control-Allow-Origin") == "*"


def test_cors_preflight(broker_proc):
    """Explicit OPTIONS routes (route resolution precedes request
    middleware, so only a real route can answer a preflight)."""
    _, _, base = broker_proc
    for path in ("/sessions", "/profiles", "/launch"):
        status, headers, _ = _request("OPTIONS", f"{base}{path}")
        assert status == 204, path
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert (headers.get("Access-Control-Allow-Methods")
                == "GET, POST, PUT, OPTIONS")
        assert (headers.get("Access-Control-Allow-Headers")
                == "Authorization, Content-Type")
        assert headers.get("Access-Control-Max-Age") == "86400"


def test_cors_on_error_paths(broker_proc):
    """Pins Sanic running response middleware on error responses (404 and
    router-405) — the login flow depends on this behavior."""
    _, _, base = broker_proc
    status, headers, _ = _request("GET", f"{base}/no-such-route")
    assert status == 404
    assert headers.get("Access-Control-Allow-Origin") == "*"
    status, headers, _ = _request("GET", f"{base}/launch")  # POST-only route
    assert status == 405
    assert headers.get("Access-Control-Allow-Origin") == "*"


def test_auto_extend_disabled(broker_proc):
    """sanic-ext, when merely installed, auto-loads and exposes an
    unauthenticated /docs + /openapi.json; AUTO_EXTEND=False pins it off
    so every install behaves like a clean one."""
    _, _, base = broker_proc
    status, _, _ = _request("GET", f"{base}/docs")
    assert status == 404
    status, _, _ = _request("GET", f"{base}/openapi.json")
    assert status == 404


def test_cors_without_token():
    """A tokenless broker now ALSO emits ACAO:* (the posture changed: see
    app.py module docstring). Token-gating CORS left a tokenless broker
    reachable over the LAN/Tailscale unable to answer the multi-host UI's
    cross-origin /sessions fetch — even though any non-browser client could
    already read it, since CORS only governs browser reads. Security still
    rests on network reachability plus the token on every mutation/data
    endpoint (/launch, /file/*, /state stay token-or-loopback gated, so a
    cross-origin page still cannot drive them)."""
    port = _free_port()
    env = dict(os.environ)
    env.pop("WEB_TERMINAL_TOKEN", None)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("BROWSERLAND_BROKER_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "webterm.broker",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while True:
            try:
                status, headers, _ = _request("GET", f"{base}/sessions")
                if status == 200:
                    break
            except OSError:
                pass
            if proc.poll() is not None:
                raise RuntimeError(f"broker exited early: {proc.returncode}")
            if time.time() > deadline:
                raise RuntimeError("broker did not come up")
            time.sleep(0.2)
        assert headers.get("Access-Control-Allow-Origin") == "*"
        # The preflight grants the same regardless of token config.
        status, headers, _ = _request("OPTIONS", f"{base}/sessions")
        assert status == 204
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert (headers.get("Access-Control-Allow-Methods")
                == "GET, POST, PUT, OPTIONS")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_profiles_endpoint(broker_proc):
    _, _, base = broker_proc
    status, _ = _http("GET", f"{base}/profiles")
    assert status == 401
    status, payload = _http("GET", f"{base}/profiles?token={TOKEN}")
    assert status == 200
    assert isinstance(payload["profiles"], list) and payload["profiles"]
    # Names only — command/cwd never leave the broker.
    assert all(isinstance(name, str) for name in payload["profiles"])
    assert payload["default"] in payload["profiles"]
    # Issues #2/#10: the host OS so the UI only sends its default start path to
    # a host whose OS matches the broker the path was configured for.
    assert payload["os"] in ("windows", "posix")


def test_title_broadcast(broker_proc):
    _, port, base = broker_proc

    async def scenario():
        producer = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await producer.send(json.dumps({
            "type": "hello", "window_id": 555002, "pid": 42,
            "title": "before", "cols": 80, "rows": 24}))
        for _ in range(50):
            status, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
            if any(s["id"] == 555002 for s in sessions):
                break
            await asyncio.sleep(0.1)

        browser = await _attach_browser(port, 555002)
        try:
            first = json.loads(await asyncio.wait_for(browser.recv(), 5))
            assert first["type"] == "resized"
            req = json.loads(await asyncio.wait_for(producer.recv(), 5))
            assert req == {"type": "snapshot_please"}

            # Producer title change -> live push to the attached browser.
            await producer.send(json.dumps(
                {"type": "title", "data": "after"}))
            frame = json.loads(await asyncio.wait_for(browser.recv(), 5))
            assert frame == {"type": "title", "data": "after"}

            status, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
            entry = next(s for s in sessions if s["id"] == 555002)
            assert entry["title"] == "after"
            assert entry["pid"] == 42
        finally:
            await browser.close()
            await producer.close()

    asyncio.run(scenario())


def test_reregister_closes_old_subscribers(broker_proc):
    _, port, base = broker_proc

    async def scenario():
        prod_a = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await prod_a.send(json.dumps({
            "type": "hello", "window_id": 555003, "pid": 1,
            "title": "a", "cols": 80, "rows": 24}))
        for _ in range(50):
            status, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
            if any(s["id"] == 555003 for s in sessions):
                break
            await asyncio.sleep(0.1)

        control, status = await _claim_lease(port)
        assert status["active"] is True
        browser = await _attach_browser(port, 555003)
        first = json.loads(await asyncio.wait_for(browser.recv(), 5))
        assert first["type"] == "resized"

        # A second producer claims the same id (agent reconnect): the stale
        # entry is replaced and its subscribers closed with 1012 so the page
        # can auto-reattach instead of sitting on a frozen socket.
        prod_b = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await prod_b.send(json.dumps({
            "type": "hello", "window_id": 555003, "pid": 2,
            "title": "b", "cols": 100, "rows": 30}))
        with pytest.raises(websockets.ConnectionClosed):
            while True:
                await asyncio.wait_for(browser.recv(), 10)
        assert browser.close_code == 1012

        # A fresh attach reaches producer B.
        browser2 = await _attach_browser(port, 555003)
        try:
            first = json.loads(await asyncio.wait_for(browser2.recv(), 5))
            assert first == {"type": "resized", "cols": 100, "rows": 30}
            req = json.loads(await asyncio.wait_for(prod_b.recv(), 5))
            assert req == {"type": "snapshot_please"}
            await browser2.send(json.dumps({"type": "input", "data": "x"}))
            frame = json.loads(await asyncio.wait_for(prod_b.recv(), 5))
            assert frame == {"type": "input", "data": "x"}
        finally:
            await browser2.close()
            await prod_a.close()
            await prod_b.close()
            await control.close()

    asyncio.run(scenario())


def test_browser_ws_requires_token(broker_proc):
    _, port, _ = broker_proc

    async def scenario():
        ws = await websockets.connect(
            f"ws://127.0.0.1:{port}/ws?session=1", max_size=None)
        try:
            with pytest.raises(websockets.ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
            assert ws.close_code == 4401
        finally:
            await ws.close()

    asyncio.run(scenario())


def test_state_requires_token(broker_proc):
    _, _, base = broker_proc
    status, _ = _http("GET", f"{base}/state")
    assert status == 401
    status, _ = _http("PUT", f"{base}/state", body=b"{}")
    assert status == 401


def test_state_roundtrip_and_conflict(broker_proc):
    _, _, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}
    # Order-independent: read the live rev (other tests may have bumped it),
    # then write baseRev=that.
    status, payload = _http("GET", f"{base}/state?token={TOKEN}")
    assert status == 200
    r0 = payload["rev"]
    assert isinstance(r0, int) and r0 >= 0
    assert isinstance(payload["settings"], dict)
    assert isinstance(payload["layout"], dict)

    body = json.dumps({"baseRev": r0,
                       "settings": {"theme": "dark"},
                       "layout": {"mode": "tiling"}}).encode()
    status, payload = _http("PUT", f"{base}/state", body=body, headers=auth)
    assert status == 200, payload
    assert payload["ok"] is True and payload["rev"] == r0 + 1

    status, payload = _http("GET", f"{base}/state?token={TOKEN}")
    assert status == 200
    assert payload["rev"] == r0 + 1
    assert payload["settings"] == {"theme": "dark"}
    assert payload["layout"] == {"mode": "tiling"}

    # Stale baseRev -> 409, with the live state inlined for one-trip resync.
    stale = json.dumps({"baseRev": r0,
                        "settings": {"theme": "light"},
                        "layout": {}}).encode()
    status, payload = _http("PUT", f"{base}/state", body=stale, headers=auth)
    assert status == 409
    assert payload["error"] == "conflict"
    assert payload["rev"] == r0 + 1
    assert payload["settings"] == {"theme": "dark"}

    # Resync on the returned rev succeeds.
    good = json.dumps({"baseRev": payload["rev"],
                       "settings": {"theme": "light"},
                       "layout": {}}).encode()
    status, payload = _http("PUT", f"{base}/state", body=good, headers=auth)
    assert status == 200 and payload["rev"] == r0 + 2


def test_state_validation(broker_proc):
    _, _, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}
    # Missing/!int baseRev.
    status, payload = _http("PUT", f"{base}/state",
                            body=json.dumps({"settings": {}, "layout": {}}
                                            ).encode(), headers=auth)
    assert status == 400 and payload["error"] == "bad_baseRev"
    # Non-object settings.
    status, payload = _http(
        "PUT", f"{base}/state",
        body=json.dumps({"baseRev": 0, "settings": [], "layout": {}}).encode(),
        headers=auth)
    assert status == 400 and payload["error"] == "bad_state"


def test_file_upload_roundtrip(broker_proc, tmp_path):
    _, _, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}
    import base64 as _b64
    name = ".webterm_upload_test.bin"
    target = REPO / name                       # default dir = broker cwd
    payload_bytes = bytes(range(256)) * 4       # non-UTF-8 binary
    b64 = _b64.b64encode(payload_bytes).decode("ascii")
    try:
        status, resp = _http(
            "POST", f"{base}/file/upload",
            body=json.dumps({"path": name, "content_b64": b64}).encode(),
            headers=auth)
        assert status == 200, resp
        assert resp["ok"] is True and resp["size"] == len(payload_bytes)
        assert target.read_bytes() == payload_bytes

        # Re-upload without overwrite -> 409.
        status, resp = _http(
            "POST", f"{base}/file/upload",
            body=json.dumps({"path": name, "content_b64": b64}).encode(),
            headers=auth)
        assert status == 409 and resp["error"] == "exists"

        # Overwrite=true succeeds.
        status, resp = _http(
            "POST", f"{base}/file/upload",
            body=json.dumps({"path": name, "content_b64": b64,
                             "overwrite": True}).encode(),
            headers=auth)
        assert status == 200 and resp["ok"] is True

        # Bad base64 -> 400.
        status, resp = _http(
            "POST", f"{base}/file/upload",
            body=json.dumps({"path": name, "content_b64": "!!!not-b64!!!",
                             "overwrite": True}).encode(),
            headers=auth)
        assert status == 400 and resp["error"] == "bad_base64"

        # Host-wide (#35): an ABSOLUTE path outside the default dir is now
        # ALLOWED (no editor_root containment) — it writes and echoes the
        # absolute path back. (pytest's tmp_path is auto-cleaned.)
        outside = (tmp_path / "uploaded_outside.bin").resolve()
        status, resp = _http(
            "POST", f"{base}/file/upload",
            body=json.dumps({"path": str(outside),
                             "content_b64": b64}).encode(),
            headers=auth)
        assert status == 200 and resp["ok"] is True, resp
        assert outside.read_bytes() == payload_bytes
        assert resp["path"] == str(outside), resp
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def test_file_api_is_host_wide(broker_proc, tmp_path):
    """#35: the file API browses the WHOLE host (same auth gate as /launch, which
    already grants shell-level filesystem access). ANY absolute path reads/writes
    — not just AGENTS.md/CLAUDE.md, and with NO live terminal required. cwd/parent
    are absolute; only malformed paths (Windows NTFS ADS) are rejected."""
    _, _, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}

    d = tmp_path / "anywhere"               # far outside the broker's default dir
    d.mkdir()
    (d / "AGENTS.md").write_text("hello agents", encoding="utf-8")
    (d / "secret.txt").write_text("a secret", encoding="utf-8")

    def _post(path, body):
        return _http("POST", f"{base}{path}",
                     body=json.dumps(body).encode(), headers=auth)

    # An ORDINARY file far outside the default dir reads (was path_outside_root)
    # and the echoed path is absolute.
    st, r = _post("/file/read", {"path": str(d / "secret.txt")})
    assert st == 200 and r["ok"] and r["content"] == "a secret", r
    assert r["path"] == str((d / "secret.txt").resolve()), r

    # AGENTS.md likewise — no carve-out, no live terminal needed.
    st, r = _post("/file/read", {"path": str(d / "AGENTS.md")})
    assert st == 200 and r["content"] == "hello agents", r

    # Write to an absolute path outside the default dir; echoes absolute path.
    st, r = _post("/file/write", {"path": str(d / "new.txt"),
                                  "content": "written"})
    assert st == 200 and r["ok"], r
    assert (d / "new.txt").read_text(encoding="utf-8") == "written"
    assert r["path"] == str((d / "new.txt").resolve()), r

    # /file/list at an absolute dir: absolute cwd + absolute parent + entries.
    st, r = _post("/file/list", {"path": str(d)})
    assert st == 200 and r["ok"], r
    assert r["cwd"] == str(d.resolve()), r
    assert r["parent"] == str(d.parent.resolve()), r
    names = {e["name"] for e in r["entries"]}
    assert {"AGENTS.md", "secret.txt", "new.txt"} <= names, names

    # A missing dir -> not_found; a file path -> not_a_directory.
    st, r = _post("/file/list", {"path": str(d / "nope")})
    assert st == 404 and r["error"] == "not_found", r
    st, r = _post("/file/list", {"path": str(d / "secret.txt")})
    assert st == 400 and r["error"] == "not_a_directory", r

    # At a filesystem ANCHOR (drive root / POSIX '/') parent is null so Up is
    # inert — the only non-trivial branch of the host-wide parent logic.
    anchor = Path(str(d.resolve())).anchor if sys.platform == "win32" else "/"
    st, r = _post("/file/list", {"path": anchor})
    assert st == 200 and r["ok"], r
    assert r["cwd"] == anchor, r
    assert r["parent"] is None, r

    # Windows NTFS alternate-data-stream spelling is still rejected.
    if sys.platform == "win32":
        st, r = _post("/file/read", {"path": str(d / "AGENTS.md") + "::$DATA"})
        assert st == 400 and r["error"] == "bad_path", r


def test_file_upload_requires_token(broker_proc):
    _, _, base = broker_proc
    status, _ = _http("POST", f"{base}/file/upload", body=b"{}")
    assert status == 401


def test_session_rpc_requires_token(broker_proc):
    _, _, base = broker_proc
    for path in ("/session/procs", "/session/kill", "/session/git"):
        status, _ = _http("POST", f"{base}{path}", body=b"{}")
        assert status == 401, path


def test_session_rpc_unknown_session(broker_proc):
    _, _, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}
    status, payload = _http(
        "POST", f"{base}/session/procs",
        body=json.dumps({"id": 99887766}).encode(), headers=auth)
    assert status == 404 and payload["error"] == "unknown_session"
    # Bad / missing id.
    status, payload = _http(
        "POST", f"{base}/session/procs",
        body=json.dumps({"id": "nope"}).encode(), headers=auth)
    assert status == 400 and payload["error"] == "bad_id"


def test_session_rpc_preflight(broker_proc):
    _, _, base = broker_proc
    for path in ("/session/procs", "/session/kill", "/session/git"):
        status, headers, _ = _request("OPTIONS", f"{base}{path}")
        assert status == 204, path
        assert headers.get("Access-Control-Allow-Origin") == "*"


def test_session_rpc_roundtrip(broker_proc):
    """procs / kill / git_status round-trips: a fake producer registers and
    answers the broker's correlated request frames; the HTTP endpoint returns
    the producer's reply. Mirrors the agent-mediated design."""
    _, port, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}
    sid = 555010

    async def scenario():
        producer = await websockets.connect(
            f"ws://127.0.0.1:{port}/browserland", max_size=None)
        await producer.send(json.dumps({
            "type": "hello", "window_id": sid, "pid": 11,
            "title": "rpc", "cols": 80, "rows": 24}))
        for _ in range(50):
            _s, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
            if any(s["id"] == sid for s in sessions):
                break
            await asyncio.sleep(0.1)

        loop = asyncio.get_running_loop()

        async def respond(expected_type, reply):
            """Read producer frames until the expected request arrives, then
            send the reply echoing its req id. Returns the request."""
            while True:
                msg = json.loads(await asyncio.wait_for(producer.recv(), 5))
                if msg.get("type") == "snapshot_please":
                    continue
                assert msg.get("type") == expected_type, msg
                await producer.send(reply(msg["req"]))
                return msg

        try:
            # --- procs ---
            fut = loop.run_in_executor(None, lambda: _http(
                "POST", f"{base}/session/procs",
                json.dumps({"id": sid}).encode(), auth))
            await respond("procs_please", lambda r: json.dumps(
                {"type": "procs", "req": r,
                 "procs": [{"pid": 1, "name": "sh"}]}))
            status, payload = await fut
            assert status == 200 and payload["ok"] is True
            assert payload["procs"] == [{"pid": 1, "name": "sh"}]

            # --- kill ---
            fut = loop.run_in_executor(None, lambda: _http(
                "POST", f"{base}/session/kill",
                json.dumps({"id": sid, "pid": 4321}).encode(), auth))
            req = await respond("kill", lambda r: json.dumps(
                {"type": "killed", "req": r, "ok": True, "pid": 4321}))
            assert req["pid"] == 4321
            status, payload = await fut
            assert status == 200 and payload["ok"] is True
            assert payload["pid"] == 4321

            # --- git ---
            fut = loop.run_in_executor(None, lambda: _http(
                "POST", f"{base}/session/git",
                json.dumps({"id": sid}).encode(), auth))
            await respond("git_status_please", lambda r: json.dumps(
                {"type": "git_status", "req": r, "ok": True,
                 "branch": "main", "dirty": False, "dirty_count": 0}))
            status, payload = await fut
            assert status == 200 and payload["ok"] is True
            assert payload["branch"] == "main"
            # Protocol envelope keys are stripped from the response.
            assert "type" not in payload and "req" not in payload
        finally:
            await producer.close()

    asyncio.run(scenario())


def test_launch_cwd_validation(broker_proc):
    """The starting-folder param is validated (existing dir) before any spawn,
    so a bad cwd is a fast 400 — no agent process is created."""
    _, _, base = broker_proc
    auth = {"Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json"}
    # Non-string cwd.
    status, payload = _http(
        "POST", f"{base}/launch",
        body=json.dumps({"profile": LAUNCH_PROFILE, "cwd": 123}).encode(),
        headers=auth)
    assert status == 400 and payload["error"] == "bad_cwd"
    # Nonexistent directory.
    status, payload = _http(
        "POST", f"{base}/launch",
        body=json.dumps({"profile": LAUNCH_PROFILE,
                         "cwd": "/no/such/dir/xyz123"}).encode(),
        headers=auth)
    assert status == 400 and payload["error"] == "cwd_not_dir"


@pytest.mark.skipif(os.name != "nt" and shutil.which("bash") is None,
                    reason="bash not installed")
def test_launch_real_agent(broker_proc):
    _, port, base = broker_proc
    status, payload = _http(
        "POST", f"{base}/launch",
        body=json.dumps({"profile": LAUNCH_PROFILE,
                         "cols": 90, "rows": 25}).encode(),
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    assert status == 200, payload
    assert payload["ok"] is True and payload["registered"] is True
    window_id = payload["id"]
    assert window_id >= (1 << 52)
    agent_pid = payload["agent_pid"]
    shell_pid = None

    try:
        status, sessions = _http("GET", f"{base}/sessions?token={TOKEN}")
        entry = next(s for s in sessions if s["id"] == window_id)
        assert entry["kind"] == "agent"
        assert entry["cols"] == 90 and entry["rows"] == 25
        assert entry["pid"] > 0
        shell_pid = entry["pid"]

        async def scenario():
            control, st = await _claim_lease(port)
            assert st["active"] is True
            browser = await _attach_browser(port, window_id)
            try:
                first = json.loads(await asyncio.wait_for(browser.recv(), 5))
                assert first == {"type": "resized", "cols": 90, "rows": 25}
                # The shell may still be printing its banner / rc noise;
                # type a marker and scan accumulated binary frames for it.
                # The \r\n ending is valid in both shells (ICRNL eats \r).
                await asyncio.sleep(1.0)
                await browser.send(json.dumps(
                    {"type": "input", "data": "echo launch_marker_77\r\n"}))
                deadline = asyncio.get_running_loop().time() + 15
                seen = b""
                while asyncio.get_running_loop().time() < deadline:
                    try:
                        frame = await asyncio.wait_for(browser.recv(), 2)
                    except asyncio.TimeoutError:
                        continue
                    if isinstance(frame, bytes):
                        seen += frame
                        if b"launch_marker_77" in seen:
                            return
                raise AssertionError(
                    f"marker never echoed; got {seen[-500:]!r}")
            finally:
                await browser.close()
                await control.close()

        asyncio.run(scenario())
    finally:
        # The agent is detached by design; reap it (and its shell) so the
        # test leaves nothing behind.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(agent_pid), "/T", "/F"],
                           capture_output=True)
        else:
            # Agent AND shell are both session leaders (start_new_session in
            # _spawn_detached and in LinuxPtyBackend.spawn) — killing the
            # agent does not take the shell down with it.
            for pid in {agent_pid, shell_pid} - {None}:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.2)  # zombies reparent to the live broker
            # subprocess; module teardown reaps them with it.
