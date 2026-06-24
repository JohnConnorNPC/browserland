"""Best-effort ``git status`` for the terminal git button (todo2 task 6).

Run ON THE AGENT (in the live shell cwd) so the result is correct for the host
the session actually lives on — a remote broker's UI sees the remote repo. The
broker only relays the request/reply (see the management-RPC path in
``registry``/``client``/``agent``).

Hardened per the design review: ``shell=False`` (argv list, no shell), a hard
timeout, ``stdin`` closed, and every interactive credential prompt disabled
(``GIT_TERMINAL_PROMPT=0`` etc.) so ``git`` can never hang waiting on input.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Any, Dict

# git status on a huge tree is still quick, but a network remote in
# ``--branch`` mode never contacts the network, so a small timeout is safe.
_TIMEOUT = 5.0
_MAX_OUTPUT = 1 * 1024 * 1024  # 1 MiB porcelain cap
_MAX_STDERR = 8 * 1024

# On Windows, a console process spawned by a windowless/detached parent (the
# agent runs DETACHED_PROCESS) gets a brand-new console window — which flashes
# on the desktop every git poll. CREATE_NO_WINDOW suppresses that window. The
# flag only exists on Windows; on POSIX getattr() yields 0, and creationflags=0
# is the default no-op, so WSL/Linux remote agents are byte-for-byte unchanged.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _capped_reader(stream, cap: int, sink: Dict[str, Any]) -> None:
    """Drain ``stream`` to completion (so the child never blocks on a full
    pipe) but only RETAIN up to ``cap`` bytes — bounds agent memory even if a
    pathological repo emits a huge porcelain dump. Never raises."""
    buf = sink["buf"]
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            room = cap - len(buf)
            if room > 0:
                buf.extend(chunk[:room])
            else:
                sink["over"] = True   # keep draining, stop storing
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _clean_env() -> Dict[str, str]:
    env = dict(os.environ)
    # Never prompt for credentials / passphrases — those would hang the agent.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "Never"
    return env


def collect(cwd: str) -> Dict[str, Any]:
    """Return a JSON-serializable status dict for the repo at ``cwd``.

    Shape on success::

        {"ok": True, "branch": "main", "detached": False,
         "ahead": 0, "behind": 0,
         "staged": 0, "unstaged": 0, "untracked": 0, "conflicts": 0,
         "dirty": False, "dirty_count": 0}

    On failure ``{"ok": False, "error": "..."}`` (not-a-repo, git missing,
    timeout, no cwd). Never raises."""
    if not cwd or not os.path.isdir(cwd):
        return {"ok": False, "error": "no_cwd"}
    argv = ["git", "-C", cwd, "status", "--porcelain=v2", "--branch"]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_clean_env(),
            shell=False,
            creationflags=_CREATE_NO_WINDOW,   # windowless on Windows; no-op elsewhere
        )
    except FileNotFoundError:
        return {"ok": False, "error": "git_not_found"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": str(exc)[:200]}

    # Drain both pipes via threads with hard byte caps (bounds memory; avoids a
    # full-pipe deadlock while we wait), then reap with a timeout.
    outd: Dict[str, Any] = {"buf": bytearray(), "over": False}
    errd: Dict[str, Any] = {"buf": bytearray(), "over": False}
    t_out = threading.Thread(target=_capped_reader,
                             args=(proc.stdout, _MAX_OUTPUT, outd), daemon=True)
    t_err = threading.Thread(target=_capped_reader,
                             args=(proc.stderr, _MAX_STDERR, errd), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return {"ok": False, "error": "timeout"}
    t_out.join(1.0)
    t_err.join(1.0)
    if proc.returncode != 0:
        err = bytes(errd["buf"]).decode("utf-8", "replace").strip()
        # The common case is "not a git repository".
        if "not a git repository" in err.lower():
            return {"ok": False, "error": "not_a_repo"}
        return {"ok": False, "error": (err[:200] or "git_error")}
    info = _parse(bytes(outd["buf"]).decode("utf-8", "replace"))
    if outd["over"]:
        info["truncated"] = True
    return info


def _parse(out: str) -> Dict[str, Any]:
    branch = ""
    detached = False
    ahead = behind = 0
    staged = unstaged = untracked = conflicts = 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
            if branch == "(detached)":
                detached = True
        elif line.startswith("# branch.ab "):
            # "# branch.ab +A -B"
            parts = line.split()
            for tok in parts[2:]:
                try:
                    if tok.startswith("+"):
                        ahead = int(tok[1:])
                    elif tok.startswith("-"):
                        behind = int(tok[1:])
                except ValueError:
                    pass
        elif line.startswith("1 ") or line.startswith("2 "):
            # Changed/renamed entry: field 2 is the two-char XY status.
            parts = line.split(" ", 2)
            if len(parts) >= 2 and len(parts[1]) == 2:
                x, y = parts[1][0], parts[1][1]
                if x != ".":
                    staged += 1
                if y != ".":
                    unstaged += 1
        elif line.startswith("u "):
            conflicts += 1
        elif line.startswith("? "):
            untracked += 1
    dirty_count = staged + unstaged + untracked + conflicts
    return {
        "ok": True,
        "branch": branch,
        "detached": detached,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "conflicts": conflicts,
        "dirty": dirty_count > 0,
        "dirty_count": dirty_count,
    }
