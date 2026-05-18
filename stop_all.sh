#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
mkdir -p logs

stop_pid_file() {
  local name="$1"
  local pid_file="$2"
  local script="$3"
  local pid=""
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -p "$pid" -o args= 2>/dev/null | grep -F "$BASE_DIR/$script" >/dev/null 2>&1; then
    kill "$pid" || true
    echo "$name encerrando (PID $pid)"
  fi
}

stop_pid_file "dashboard" "logs/monitor_web.pid" "monitor_web.py"
stop_pid_file "paper_loop" "logs/paper_loop.pid" "paper_loop.sh"
stop_pid_file "LLM" "logs/llm_server.pid" "llm_server_qwen.py"

sleep 1

pkill -f "$BASE_DIR/monitor_web.py" || true
pkill -f "$BASE_DIR/paper_loop.sh" || true
pkill -f "$BASE_DIR/llm_server_qwen.py" || true
rm -f logs/paper_loop.lock logs/paper_loop.pid logs/monitor_web.pid logs/llm_server.pid || true

echo "Servicos encerrados (se estavam ativos)."
