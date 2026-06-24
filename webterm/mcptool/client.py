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
    "too_large": "Input payload exceeds the broker's 256 KiB limit.",
    "no_producer_rpc": "Terminal did not answer the screen-read request "
                       "(a non-agent producer or a timeout).",
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

    def read_screen(self, id: int) -> Dict[str, Any]:
        """Render a terminal's screen as plain text."""
        return self._post("/mcp/read", {"id": id}, timeout=self._read_timeout)

    def send_input(self, id: int, data: str) -> Dict[str, Any]:
        """Type into a terminal. Requires the window be in ``readwrite`` mode."""
        return self._post("/mcp/input", {"id": id, "data": data})

    def launch_terminal(self, profile: Optional[str] = None, cols: int = 80,
                        rows: int = 24, title: Optional[str] = None,
                        cwd: Optional[str] = None) -> Dict[str, Any]:
        """Spawn a new terminal from a profile. Requires ``allow_launch``."""
        body: Dict[str, Any] = {"cols": cols, "rows": rows}
        for k, v in (("profile", profile), ("title", title), ("cwd", cwd)):
            if v is not None:
                body[k] = v
        return self._post("/mcp/launch", body)
