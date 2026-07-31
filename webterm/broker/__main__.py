"""``python -m webterm.broker --host 127.0.0.1 --port 4445``"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import auth, supervise
from .app import DEFAULT_PORT, _open_url, create_app, load_config


def _print_token(config: dict, port: int) -> int:
    """``--print-token``: report the token WITHOUT starting a server and without
    minting one. Asking for the token must never be the thing that creates it —
    a mint here would hand out a value the broker isn't running.

    Resolution mirrors the broker's own: $WEB_TERMINAL_TOKEN, then
    broker_config's ``auth_token``, then the sidecar beside the state store."""
    state_path = Path(
        config.get("state_path") or (Path(os.getcwd()) / "webterm_state.json")
    ).resolve()
    path = Path(
        config.get("auth_state_path")
        or (state_path.parent / auth.AUTH_STATE_FILENAME)).resolve()
    token = auth.resolve_existing_token(config, path)
    if not token:
        print(
            f"no auth token yet: nothing in ${auth.TOKEN_ENV}, no auth_token "
            f"in broker_config, and no {path}.\n"
            "Start the broker once - it mints one - or set auth_token "
            "yourself.", file=sys.stderr)
        return 1
    print(token)
    print(_open_url(config, port, token))
    return 0


def _scan_recordings(config: dict, ns) -> int:
    """``--scan-recordings``: audit committed recordings for secrets (#145).

    Exit 0 = clean, 1 = findings, 2 = the audit could not be completed (so an
    error can never be mistaken for a clean bill of health)."""
    from . import recscan

    state_path = Path(
        config.get("state_path") or (Path(os.getcwd()) / "webterm_state.json")
    ).resolve()
    rec_dir = Path(
        ns.recordings_dir or config.get("recordings_dir")
        or (state_path.parent / "webterm_recordings")).resolve()

    # Read-only resolution: auditing must never MINT a token (that would hand
    # back a value the broker isn't running and search for the wrong thing).
    secrets = {}
    token = auth.resolve_existing_token(
        config,
        Path(config.get("auth_state_path")
             or (state_path.parent / auth.AUTH_STATE_FILENAME)).resolve())
    if token:
        secrets["auth_token"] = token
    mcp = auth.resolve_mcp_token(config)
    if mcp:
        secrets["mcp_token"] = mcp
    for i, extra in enumerate(ns.secret or []):
        secrets[f"--secret[{i}]"] = extra

    if not secrets:
        print("nothing to search for: no auth_token, no mcp_token, and no "
              "--secret given. Pass --secret VALUE (e.g. a ROTATED token that "
              "is no longer configured but may still be in old recordings).",
              file=sys.stderr)
        return 2

    result = recscan.scan_dir(rec_dir, secrets)
    if ns.json:
        print(json.dumps({
            "recordingsDir": str(rec_dir),
            "scannedFor": sorted(secrets),
            "findings": [{"file": str(f.path), "secret": f.label,
                          "eventIndex": f.event_index, "timestampMs": f.t_ms,
                          "clock": f.clock,
                          "spansEvents": f.spans_events} for f in result.findings],
            "errors": result.errors,
        }, indent=2))
    else:
        print(f"scanning {rec_dir} for: {', '.join(sorted(secrets))}")
        for f in result.findings:
            where = ("meta/title" if f.event_index == recscan.META_INDEX
                     else f"event {f.event_index} at {f.clock}")
            span = " (split across output events)" if f.spans_events else ""
            print(f"  FOUND {f.label} in {f.path.name} -- {where}{span}")
        for e in result.errors:
            print(f"  ERROR {e}", file=sys.stderr)
        n = len(result.findings)
        if n:
            files = sorted({f.path.name for f in result.findings})
            print(f"\n{n} finding(s) in {len(files)} recording(s): "
                  f"{', '.join(files)}")
            print("These recordings contain a live secret. Delete them "
                  "(Session recorder -> the recording -> x, twice) or keep "
                  "them off anywhere shared.")
        elif not result.errors:
            print("\nno configured secret found in any recording.")
        # The honest caveat, printed on a CLEAN result too -- without it this
        # tool recreates the false all-clear it exists to prevent.
        print("\nNOTE: this finds a secret only where it appears as CONTIGUOUS "
              "bytes in the recorded output. A secret the terminal broke up -- "
              "interleaved with colour/cursor escapes, redrawn by the shell as "
              "you typed, or echoed a character at a time -- will NOT be found, "
              "and a rotated secret that is no longer configured is only "
              "searched for if you pass it with --secret. A clean result is "
              "evidence, not proof.")
    if result.errors:
        return 2
    return 1 if result.findings else 0


def _run_worker(config: dict, host: str, port: int) -> int:
    """The server itself -- what ``python -m webterm.broker`` used to be, now
    reached only via the internal ``--worker`` flag (#183).

    Two things happen here that did not before:

    * the exit code comes from the app. With ``single_process=True``,
      ``app.stop()`` from a request handler makes ``app.run()`` RETURN, so a
      route can set ``app.ctx.exit_code`` and have this process exit with it --
      which is how a restart is requested of the supervisor. Read defensively:
      this must work before (and after) any route that sets it exists.
    * a port clash is reported as its OWN exit code rather than as a generic
      crash, so the supervisor can refuse to relaunch into it.
    """
    app = create_app(config, port=port)
    # Did the server ever come up? This is what keeps the bind classification
    # honest: EACCES is BOTH "the OS refused this address" and "a file write was
    # denied", so an errno alone would let a mid-flight permission error at
    # runtime masquerade as a port clash - stopping the broker for good with a
    # message about ports. A failure that happens before this listener ever ran
    # is, by construction, a startup failure.
    started = {"ok": False}

    async def _mark_started(_app, _loop):
        started["ok"] = True

    app.register_listener(_mark_started, "after_server_start")
    try:
        # single_process avoids Sanic's multi-worker spawn path, which on
        # Windows re-imports the module and can't find an app built in main();
        # it also dodges an os.killpg call that doesn't exist on Windows.
        app.run(host=host, port=port, single_process=True)
    except Exception as exc:      # noqa: BLE001 -- re-raised unless it's a bind
        # Not `except OSError`: Sanic wraps the bind failure in its own
        # ServerError, so the errno lives in the exception's context chain
        # rather than on the exception itself (bind_failure_reason walks it).
        reason = (None if started["ok"]
                  else supervise.bind_failure_reason(exc))
        if reason == supervise.BIND_IN_USE:
            print(f"cannot bind {host}:{port} - the address is already in "
                  f"use. Another broker (or something else) is listening "
                  f"there; stop it, or start this one on a different --port.",
                  file=sys.stderr)
            return supervise.EXIT_ADDR_IN_USE
        if reason == supervise.BIND_DENIED:
            print(f"cannot bind {host}:{port} - the OS refused the address. "
                  f"Usually that means it is already taken (Windows reports an "
                  f"occupied port this way), or the port is reserved/"
                  f"privileged for this user. Try a different --port.",
                  file=sys.stderr)
            return supervise.EXIT_ADDR_IN_USE
        raise
    try:
        return int(getattr(app.ctx, "exit_code", 0) or 0)
    except (TypeError, ValueError):
        # A garbage exit_code must not turn a clean shutdown into a crash.
        return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("WEBTERM_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        prog="python -m webterm.broker",
        description="webterm broker: relays browser WSes to PTY agents / "
                    "terminal windows.",
    )
    parser.add_argument("--host", default=None,
                        help="bind address (default: config or 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"bind port (default: config or {DEFAULT_PORT}; "
                             f"4444 was an earlier broker's port)")
    parser.add_argument("--config", default=None,
                        help="path to broker_config.json "
                             "(default: $WEB_TERMINAL_CONFIG, then repo root)")
    parser.add_argument("--headless", action="store_true",
                        help="serve the JSON/WS API only — no desktop page or "
                             "help corpus (overrides serve_ui in config)")
    parser.add_argument("--print-token", action="store_true",
                        help="print this broker's auth token and the URL to "
                             "open, then exit (never mints one)")
    parser.add_argument("--scan-recordings", action="store_true",
                        help="audit saved session recordings for this broker's "
                             "secrets, then exit (exit 1 = found, 2 = the audit "
                             "could not be completed)")
    parser.add_argument("--recordings-dir", default=None,
                        help="directory to scan (default: the configured "
                             "recordings dir beside the state store)")
    parser.add_argument("--secret", action="append", default=None,
                        help="extra literal to search for; repeatable. Use it "
                             "for a ROTATED token that is no longer configured "
                             "but may still sit in an old recording")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output for --scan-recordings")
    # Internal (#183): this process is the SUPERVISOR unless told otherwise.
    # SUPPRESSed from --help because it is not something a human should pass --
    # a hand-run `--worker` is a broker that cannot restart itself, which is
    # exactly the state the supervisor exists to remove.
    parser.add_argument("--worker", action="store_true",
                        help=argparse.SUPPRESS)
    ns = parser.parse_args()

    config = load_config(ns.config)
    host = ns.host or config.get("host") or "127.0.0.1"
    port = ns.port or int(config.get("port") or DEFAULT_PORT)
    # CLI wins over config, matching --host/--port precedence. Fold into the
    # config dict so create_app's single config.get("serve_ui") read is the one
    # source of truth. There's no --no-headless; to default headless use the key.
    if ns.headless:
        config["serve_ui"] = False
    # Same folding for the resolved host, so the startup banner and
    # --print-token can build the ready-to-open URL off the config alone.
    config["host"] = host

    # The one-shot subcommands answer and exit. They deliberately run BEFORE
    # the supervisor split: asking for a token must not spawn a broker, and
    # auditing recordings must not sit under a process that would relaunch it.
    if ns.print_token:
        return _print_token(config, port)
    if ns.scan_recordings:
        return _scan_recordings(config, ns)

    if ns.worker:
        return _run_worker(config, host, port)
    # No --worker: be the supervisor. sys.argv[1:] (not the parsed namespace)
    # is replayed to the child, so the worker resolves its own config exactly
    # as this process just did -- no second, drifting serialization of the CLI.
    return supervise.supervise(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
