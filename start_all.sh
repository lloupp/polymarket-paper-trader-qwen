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

LLM_PORT="${LLM_PORT:-8080}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8090}"

if curl -sS --max-time 3 "http://127.0.0.1:${LLM_PORT}/health" >/dev/null 2>&1; then
  echo "LLM já está respondendo na porta ${LLM_PORT}"
elif pgrep -f "python3 .*llm_server_qwen.py" >/dev/null; then
  echo "LLM (llm_server_qwen.py) já está rodando"
else
  nohup python3 llm_server_qwen.py > logs/llm_server.log 2>&1 &
  echo $! > logs/llm_server.pid
  echo "LLM iniciado (PID $(cat logs/llm_server.pid))"
fi

if pgrep -f "bash .*paper_loop.sh" >/dev/null; then
  echo "paper_loop já está rodando"
else
  nohup bash paper_loop.sh > logs/paper_loop.out 2>&1 &
  echo $! > logs/paper_loop.pid
  echo "paper_loop iniciado (PID $(cat logs/paper_loop.pid))"
fi

if pgrep -f "python3 .*monitor_web.py" >/dev/null; then
  echo "dashboard já está rodando"
else
  nohup python3 monitor_web.py > logs/monitor_web.log 2>&1 &
  echo $! > logs/monitor_web.pid
  echo "dashboard iniciado (PID $(cat logs/monitor_web.pid))"
fi

echo "\nHealth checks:"
curl -sS --max-time 6 "http://127.0.0.1:${LLM_PORT}/health" || true
echo
curl -sS --max-time 6 "http://127.0.0.1:${DASHBOARD_PORT}/health" || true
echo

echo "Dashboard: http://127.0.0.1:${DASHBOARD_PORT}"
