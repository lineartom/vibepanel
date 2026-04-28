#!/usr/bin/env bash
# Usage:
#   ./start.sh                          # tmux session named "minecraft", port 8080
#   TMUX_TARGET=myserver ./start.sh
#   ./start.sh --session myserver --port 9000

set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv --upgrade-deps 
.venv/bin/pip install -q -r requirements.txt

exec .venv/bin/python server.py "$@"
