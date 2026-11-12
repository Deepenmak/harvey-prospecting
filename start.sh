#!/bin/sh
# Entrypoint: start dashboard (background) then heartbeat (foreground).
# If the heartbeat exits for any reason, kill the dashboard so Docker
# restarts the whole container cleanly.
set -e

echo "[sellbird] Starting dashboard..."
python -m sellbird.cli dashboard --host 0.0.0.0 --port 5555 &
DASHBOARD_PID=$!

# Give uvicorn a moment to bind before the heartbeat begins.
sleep 3

echo "[sellbird] Starting heartbeat..."
python -m sellbird.cli run || true

echo "[sellbird] Heartbeat exited — shutting down."
kill "$DASHBOARD_PID" 2>/dev/null || true
wait "$DASHBOARD_PID" 2>/dev/null || true
