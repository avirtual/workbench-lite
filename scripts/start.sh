#!/bin/bash
# Start Workbench Lite server
set -e
cd "$(dirname "$0")/.."

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
fi

PORT="${WORKBENCH_PORT:-9800}"

# Check if already running
if lsof -i :"$PORT" -sTCP:LISTEN > /dev/null 2>&1; then
    echo "Server already running on port $PORT"
    echo "Use ./scripts/restart.sh to restart, or ./scripts/stop.sh to stop."
    exit 1
fi

# Activate venv if present
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
fi

echo "Starting Workbench Lite on http://127.0.0.1:$PORT"
python3 workbench.py
