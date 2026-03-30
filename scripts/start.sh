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

# Preflight checks
echo "Preflight checks..."
FAIL=0
command -v python3 > /dev/null 2>&1 && python3 -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null \
    && echo "  ✓ Python 3.11+" || { echo "  ✗ Python 3.11+ required"; FAIL=1; }
command -v tmux > /dev/null 2>&1 \
    && echo "  ✓ tmux" || { echo "  ✗ tmux not found (brew install tmux)"; FAIL=1; }
command -v claude > /dev/null 2>&1 \
    && echo "  ✓ Claude Code CLI" || { echo "  ✗ Claude Code CLI not found"; FAIL=1; }
python3 -c 'import fastapi, uvicorn, mcp, dotenv' 2>/dev/null \
    && echo "  ✓ Python dependencies" || { echo "  ✗ Run: pip install -r requirements.txt"; FAIL=1; }
[ $FAIL -eq 0 ] || { echo "Fix the issues above before starting."; exit 1; }

echo "Starting Workbench Lite on http://127.0.0.1:$PORT"
python3 workbench.py
