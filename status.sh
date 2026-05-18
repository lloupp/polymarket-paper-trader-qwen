#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

LLM_PORT="${LLM_PORT:-8080}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8090}"
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
PAPER_LLM_ENABLED="${PAPER_LLM_ENABLED:-0}"
PAPER_LLM_SERVER_ENABLED="${PAPER_LLM_SERVER_ENABLED:-$PAPER_LLM_ENABLED}"

pid_status() {
  local name="$1"
  local pid_file="$2"
  local script="$3"
  local pid=""
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= 2>/dev/null | grep -F "$BASE_DIR/$script" >/dev/null 2>&1; then
    echo "$name: rodando (PID $pid)"
  else
    echo "$name: parado"
  fi
}

echo "=== processos ==="
if [ "$PAPER_LLM_SERVER_ENABLED" = "1" ]; then
  pid_status "LLM" "logs/llm_server.pid" "llm_server_qwen.py"
else
  echo "LLM: desabilitado"
fi
pid_status "paper_loop" "logs/paper_loop.pid" "paper_loop.sh"
pid_status "dashboard" "logs/monitor_web.pid" "monitor_web.py"

printf "\n=== health LLM ===\n"
if [ "$PAPER_LLM_SERVER_ENABLED" = "1" ]; then
  curl -sS --max-time 6 "http://${HEALTH_HOST}:${LLM_PORT}/health" || true
else
  echo '{"ok":true,"llm_server":"disabled"}'
fi

printf "\n=== health dashboard ===\n"
curl -sS --max-time 6 "http://${HEALTH_HOST}:${DASHBOARD_PORT}/health" || true

echo
