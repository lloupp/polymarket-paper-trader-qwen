#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
mkdir -p logs

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x "$BASE_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$BASE_DIR/.venv/bin/python"
fi

if [ -f logs/monitor_web.pid ]; then
  OLD_PID="$(cat logs/monitor_web.pid 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" || true
    sleep 1
  fi
fi

pkill -f "[p]ython.*monitor_web.py" || true
rm -f logs/monitor_web.pid

nohup "$PYTHON_BIN" monitor_web.py > logs/monitor_web.log 2>&1 &
echo $! > logs/monitor_web.pid

echo "dashboard reiniciado (PID $(cat logs/monitor_web.pid))"
echo "Dashboard: http://127.0.0.1:${DASHBOARD_PORT:-8090}"
