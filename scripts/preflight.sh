#!/bin/bash
# Basic Workbench — Preflight Checks
# Run before starting the server to verify all prerequisites.

set -e

PASS=0
FAIL=0

check() {
    local label="$1"
    local cmd="$2"
    local fix="$3"

    if eval "$cmd" > /dev/null 2>&1; then
        echo "  ✓ $label"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $label"
        echo "    Fix: $fix"
        FAIL=$((FAIL + 1))
    fi
}

echo "Basic Workbench — Preflight Checks"
echo "==================================="
echo ""

# Python 3.11+
check "Python 3.11+" \
    "python3 -c 'import sys; assert sys.version_info >= (3, 11)'" \
    "Install Python 3.11 or newer: https://python.org"

# tmux
check "tmux installed" \
    "command -v tmux" \
    "Install tmux: brew install tmux (macOS) or apt install tmux (Linux)"

# Claude Code CLI
check "Claude Code CLI installed" \
    "command -v claude" \
    "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code"

# Port availability
PORT="${WORKBENCH_PORT:-9800}"
check "Port $PORT available" \
    "! lsof -i :$PORT -sTCP:LISTEN" \
    "Port $PORT is in use. Set WORKBENCH_PORT to use a different port."

# Python dependencies
check "Python dependencies installed" \
    "python3 -c 'import fastapi; import uvicorn; import mcp'" \
    "Run: pip install -r requirements.txt"

echo ""
echo "---"
if [ $FAIL -eq 0 ]; then
    echo "All $PASS checks passed! Run: python3 workbench.py"
else
    echo "$PASS passed, $FAIL failed. Fix the issues above before starting."
    exit 1
fi
