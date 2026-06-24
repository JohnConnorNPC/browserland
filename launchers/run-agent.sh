#!/usr/bin/env bash
# Run a webterm agent on Linux. Bootstraps a venv beside the repo on first
# run.
#
#   BROKER_URL=ws://host:4445/browserland WEB_TERMINAL_TOKEN=... \
#       ./launchers/run-agent.sh [agent opts] [-- command...]
#
# Defaults: broker ws://127.0.0.1:4445/browserland, command bash -l.
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
# WEB_TERMINAL_TOKEN is picked up from the environment if set.

if [ "$#" -gt 0 ]; then
    exec "$VENV_DIR/bin/python3" -m webterm.agent "$@"
else
    exec "$VENV_DIR/bin/python3" -m webterm.agent -- bash -l
fi

# Installable systemd units: see launchers/systemd/
