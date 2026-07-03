"""Thin HTTP client over the Browserland broker's ``/mcp/*`` interface.

One method per endpoint (mirrors the broker contract documented in the root
README's "MCP HTTP interface" section). Authenticates with the **MCP token**
(the secret in ``webterm_mcp.json``) via an ``Authorization: Bearer`` header on
a persistent :class:`httpx.Client`.

Non-2xx responses carry a ``{"error": "<code>"}`` body; :meth:`BrowserlandClient`
parses that and raises :class:`BrowserlandError` with a human-readable message so an
MCP client surfaces a readable tool error instead of a raw stack trace.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

# Broker error codes -> human-readable messages. Source of truth is the error
# table in the root README ("MCP HTTP interface" > "Error reference"). Codes not
# listed here fall back to the raw code so a new broker error is never swallowed.
_ERROR_MESSAGES = {
    "mcp_disabled": "MCP interface is disabled on the broker "
                    "(enable it and set a token in Control Panel → MCP access).",
    "auth_required": "MCP token missing or invalid.",
    "bad_json": "Broker rejected the request body as malformed JSON.",
    "bad_id": "Window id is missing or not an integer.",
    "unknown_or_off": "No such terminal, or its MCP access mode is 'off'.",
    "bad_data": "Input 'data' must be a string.",
    "read_only": "Terminal is not in 'readwrite' mode "
                 "(promote it via the window's MCP access menu).",
    "bad_pace": "'pace_ms' must be an integer.",
    "too_large": "Input payload exceeds the broker's 256 KiB limit.",
    "no_producer_rpc": "Terminal did not answer the screen-read request "
                       "(a non-agent producer or a timeout).",
    "reset_failed": "The terminal's agent failed to clear its screen buffer.",
    "flush_failed": "The terminal's agent failed to flush its pending input.",
    "launch_disabled": "Launching is disabled on the broker "
                       "(enable 'allow_launch' in Control Panel → MCP access).",
    "unknown_profile": "No such launch profile (see list_profiles).",
    "bad_dims": "Invalid 'cols'/'rows'.",
    "bad_cwd": "Invalid 'cwd'.",
    "cwd_not_dir": "'cwd' is not an existing directory.",
    "too_many_pending_launches": "Broker is busy with too many pending launches; retry shortly.",
    "spawn_failed": "Broker failed to spawn the agent.",
    "agent_exited_early": "The spawned agent exited before registering.",
}


class BrowserlandError(RuntimeError):
    """A broker ``/mcp/*`` call failed.

    ``status`` is the HTTP status code; ``code`` is the broker's ``error`` value
    (e.g. ``"read_only"``), or ``None`` when the body had no parseable error.
    """

    def __init__(self, status: int, code: Optional[str], message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class BrowserlandClient:
    """Thin wrapper over the broker's ``/mcp/*`` HTTP interface."""

    def __init__(self, base: str = "http://127.0.0.1:4445", token: str = "",
                 timeout: float = 10.0, read_timeout: float = 30.0,
                 transport: Optional[httpx.BaseTransport] = None):
        self.base = base.rstrip("/")
        # `read` does a producer round-trip on the broker, so it gets a longer
        # timeout (applied per-request in read_screen); everything else uses the
        # short default so a stalled broker doesn't hang a tool call for 30s.
        self._read_timeout = read_timeout
        # `transport` is an injection seam for tests (httpx.MockTransport); in
        # production it stays None and httpx uses its default network transport.
        self._client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
        )

    # --- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BrowserlandClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- low-level --------------------------------------------------------
    def _raise_for_status(self, r: httpx.Response) -> None:
        if r.is_success:
            return
        code: Optional[str] = None
        try:
            body = r.json()
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                code = body["error"]
        except ValueError:
            pass
        if code is not None:
            message = _ERROR_MESSAGES.get(code, f"Broker error: {code}")
            message = f"{message} (HTTP {r.status_code})"
        else:
            message = f"Broker returned HTTP {r.status_code} with no error code."
        raise BrowserlandError(r.status_code, code, message)

    def _request(self, method: str, path: str, *, json_body: Any = None,
                 timeout: Any = httpx.USE_CLIENT_DEFAULT) -> Any:
        # A connection refused / DNS / TLS / timeout error never produces an
        # HTTP response, so it can't carry a broker `error` code — translate it
        # into a BrowserlandError too, so callers only ever see BrowserlandError.
        try:
            r = self._client.request(method, self.base + path,
                                     json=json_body, timeout=timeout)
        except httpx.RequestError as exc:
            raise BrowserlandError(
                0, "connection_error",
                f"Cannot reach the Browserland broker at {self.base}: {exc}",
            ) from exc
        self._raise_for_status(r)
        return r.json()

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def _post(self, path: str, body: Dict[str, Any],
              timeout: Any = httpx.USE_CLIENT_DEFAULT) -> Any:
        return self._request("POST", path, json_body=body, timeout=timeout)

    # --- one method per endpoint; each maps 1:1 to an MCP tool ------------
    def info(self) -> Dict[str, Any]:
        """Feature flags: ``allow_launch`` + ``default_mode``."""
        return self._get("/mcp/info")

    def list_terminals(self) -> List[Dict[str, Any]]:
        """MCP-visible terminals (windows in ``off`` mode are omitted)."""
        return self._get("/mcp/terminals")

    def list_profiles(self) -> Dict[str, Any]:
        """Launchable profile names + the broker default."""
        return self._get("/mcp/profiles")

    def read_screen(self, id: int, view: str = "screen", lines: int = 0,
                    wait_for_change: Optional[str] = None,
                    timeout_ms: int = 0,
                    wait_for_text: Optional[str] = None,
                    wait_for_regex: Optional[str] = None,
                    wait_absent: bool = False,
                    since: Optional[str] = None,
                    attrs: bool = False,
                    wait_for_idle: int = 0) -> Dict[str, Any]:
        """Render a terminal's screen as plain text. ``view="scrollback"`` with
        ``lines>0`` prepends that many lines of history above the grid (#21).

        ``wait_for_change`` (a prior ``content_hash``) + ``timeout_ms`` hold the
        read until the screen changes or the timeout elapses (#26).
        ``wait_for_text`` / ``wait_for_regex`` (+ ``wait_absent``) instead hold
        until the screen contains (or no longer contains) the match (#51), and
        the reply carries ``matched``. ``wait_for_idle`` (ms) instead holds until
        the CURSOR-BLIND screen hash (``stable_hash``) has been unchanged for that
        many ms — output went quiet — or the timeout elapses; the reply carries
        ``matched`` and every reply carries ``stable_hash`` (the blink-insensitive
        digest) (#135). ``since`` (a prior ``content_hash``)
        requests a delta: the reply carries ``changed_rows`` + ``delta`` instead
        of the full grid when the agent can diff it (#52). ``attrs`` adds
        ``attr_runs`` — the styled fg/bg/reverse cell runs — so a color-only menu
        selection the plain text drops is visible (#128). ``partial`` (present
        and true only when it applies, #130) flags a valid but possibly
        incomplete alt-screen grid whose one-time full-frame paint was lost to
        ring eviction; distinct from ``degraded`` and self-healing. ``idle_ms``
        (#133) is ms since the terminal last emitted output — best-effort, absent
        from older agents, and UNRELIABLE for a perpetually-animating app (Dwarf
        Fortress paints every frame, so its idle_ms never grows); for those,
        pacing/flush and a semantic screen check are the real settle signals. The
        HTTP read timeout is stretched past the broker's wait so it doesn't give
        up early."""
        body: Dict[str, Any] = {"id": id}
        if view and view != "screen":
            body["view"] = view
        if lines:
            body["lines"] = lines
        if attrs:
            body["attrs"] = True
        req_timeout = self._read_timeout
        waiting = bool(wait_for_change or wait_for_text or wait_for_regex
                       or wait_for_idle)
        if wait_for_change:
            body["wait_for_change"] = wait_for_change
        if wait_for_text:
            body["wait_for_text"] = wait_for_text
        if wait_for_regex:
            body["wait_for_regex"] = wait_for_regex
        if wait_for_idle:
            body["wait_for_idle"] = int(wait_for_idle)
        if wait_absent:
            body["wait_absent"] = True
        if since:
            body["since"] = since
        if waiting and timeout_ms:
            body["timeout_ms"] = int(timeout_ms)
            req_timeout = self._read_timeout + int(timeout_ms) / 1000.0
        return self._post("/mcp/read", body, timeout=req_timeout)

    def send_input(self, id: int, data: str) -> Dict[str, Any]:
        r"""Type into a terminal. Requires the window be in ``readwrite`` mode.

        ``data`` is sent **verbatim** — this is the thin 1:1 endpoint wrapper.
        Mapping a logical Enter to a carriage return is the *tool's* job
        (:func:`server._newlines_to_enter`); a caller here that wants Enter to
        submit on PowerShell must pass ``\r`` itself (see issue #13)."""
        return self._post("/mcp/input", {"id": id, "data": data})

    def reset_terminal(self, id: int) -> Dict[str, Any]:
        """Clear a terminal's screen-render buffer (#27). Requires ``readwrite``
        mode. Wipes the agent's PTY-output ring so the next ``read_screen``
        starts from a clean slate; does not touch the running app."""
        return self._post("/mcp/reset", {"id": id})

    def flush_input(self, id: int) -> Dict[str, Any]:
        """Discard keystrokes queued to a terminal's app but not yet consumed
        (#133). Requires ``readwrite`` mode. The INPUT-side mirror of
        :meth:`reset_terminal`: reset clears the screen-render buffer, this drops
        a pending input backlog (e.g. a runaway ``send_keys`` burst) so the app
        can settle. Does not touch the app's already-drawn screen; a best-effort
        no-op on a Windows/ConPTY agent (no input-queue flush primitive)."""
        return self._post("/mcp/flush", {"id": id})

    def set_pace(self, id: int, pace_ms: int) -> Dict[str, Any]:
        """Set a terminal's DEFAULT inter-key send_keys pacing (#133). Requires
        ``readwrite`` mode. Broker-LOCAL state (no producer round-trip, unlike
        reset/flush): the broker stamps it on the window and surfaces it via
        ``list_terminals``, and the MCP server's send_keys reads it so a call
        with no explicit ``delay_ms`` auto-paces. 0 disables (single burst); the
        broker clamps to its cap. Ephemeral per-connection (resets on relaunch)."""
        return self._post("/mcp/pace", {"id": id, "pace_ms": pace_ms})

    def launch_terminal(self, profile: Optional[str] = None, cols: int = 80,
                        rows: int = 24, title: Optional[str] = None,
                        cwd: Optional[str] = None) -> Dict[str, Any]:
        """Spawn a new terminal from a profile. Requires ``allow_launch``."""
        body: Dict[str, Any] = {"cols": cols, "rows": rows}
        for k, v in (("profile", profile), ("title", title), ("cwd", cwd)):
            if v is not None:
                body[k] = v
        return self._post("/mcp/launch", body)
