#!/usr/bin/env bash
# Run a webterm agent on Linux. Bootstraps a venv beside the repo on first
# run.
#
#   BROKER_URL=ws://host:4445/browserland WEB_TERMINAL_TOKEN=... \
#       ./launchers/run-agent.sh [agent opts] [-- command...]
#
# Defaults: broker ws://127.0.0.1:4445/browserland, command bash -l.
# The token is REQUIRED (#142) - unset, it is read from the repo-root
# webterm_token.json the broker mints. See docs/UPGRADING.md.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${VENV_DIR:-$REPO_DIR/.venv}"

if [ ! -x "$VENV_DIR/bin/python3" ]; then
    echo "[run-agent] creating venv at $VENV_DIR" >&2
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet "websockets>=12"
fi

export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# The agent reads $BROWSERLAND_BROKER_URL above its --broker-url flag.
export BROWSERLAND_BROKER_URL="${BROWSERLAND_BROKER_URL:-${BROKER_URL:-ws://127.0.0.1:4445/browserland}}"

# A token is REQUIRED on every connection, including loopback (#142). Fall back
# to the sidecar the broker mints beside its state store so a hand-started agent
# on the same box needs no setup; fail loudly rather than dialling a broker that
# will only close us with 4401.
if [ -z "${WEB_TERMINAL_TOKEN:-}" ]; then
    TOKEN_FILE="${WEB_TERMINAL_TOKEN_FILE:-$REPO_DIR/webterm_token.json}"
    if [ -r "$TOKEN_FILE" ]; then
        WEB_TERMINAL_TOKEN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["auth_token"])' "$TOKEN_FILE" 2>/dev/null || true)"
        export WEB_TERMINAL_TOKEN
    fi
fi
if [ -z "${WEB_TERMINAL_TOKEN:-}" ]; then
    echo "[run-agent] no broker token. Set WEB_TERMINAL_TOKEN (or point" >&2
    echo "            WEB_TERMINAL_TOKEN_FILE at the broker's webterm_token.json)." >&2
    echo "            Print it with: python3 -m webterm.broker --print-token" >&2
    exit 1
fi

if [ "$#" -gt 0 ]; then
    exec "$VENV_DIR/bin/python3" -m webterm.agent "$@"
else
    exec "$VENV_DIR/bin/python3" -m webterm.agent -- bash -l
fi

# Installable systemd units: see launchers/systemd/
