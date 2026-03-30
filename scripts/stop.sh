#!/bin/bash
# Stop Workbench Lite server and all activity parsers
cd "$(dirname "$0")/.."

if [ -f .env ]; then
    set -a; source .env; set +a
fi

PORT="${WORKBENCH_PORT:-9800}"

# Kill server by port
PID=$(lsof -t -i :"$PORT" -sTCP:LISTEN 2>/dev/null)
if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null
    echo "Server stopped (PID $PID)"
else
    echo "No server running on port $PORT"
fi

# Kill activity parsers
pkill -f "activity_parser.py.*--sessions-dir.*/tmp/basic-wb-sessions" 2>/dev/null && echo "Activity parsers stopped" || true
