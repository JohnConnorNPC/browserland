#!/usr/bin/env bash
# Run the webterm broker on Linux.
#
#   WEB_TERMINAL_TOKEN=... ./launchers/run-broker.sh [--host H] [--port P]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo "[run-broker] creating venv at $VENV_DIR" >&2
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet "websockets>=12" "sanic>=23"
fi

export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# WEB_TERMINAL_TOKEN / WEB_TERMINAL_CONFIG are picked up from the environment.
exec "$VENV_DIR/bin/python3" -m webterm.broker "$@"
