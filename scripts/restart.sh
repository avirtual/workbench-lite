#!/bin/bash
# Restart Workbench Lite server (preserves agents — they stay alive in tmux)
set -e
cd "$(dirname "$0")/.."

echo "Stopping server..."
./scripts/stop.sh

echo "Waiting for port to free..."
sleep 1

echo "Starting server..."
./scripts/start.sh
