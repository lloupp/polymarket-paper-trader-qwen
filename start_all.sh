#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
mkdir -p logs

STARTUP_TIMEOUT="${STARTUP_TIMEOUT:-45}"
HEALTH_HOST="${HEALTH_HOST:-127.0.0.1}"
PAPER_SKIP_BOOTSTRAP="${PAPER_SKIP_BOOTSTRAP:-0}"

# Evita start concorrente (ex.: multiplos cliques no dashboard).
exec 9>"$BASE_DIR/logs/start_all.lock"
if ! flock -n 9; then
  echo "start_all ja em execucao; ignorando chamada concorrente"
  exit 0
fi

if [ "$PAPER_SKIP_BOOTSTRAP" != "1" ]; then
  ./bootstrap.sh
fi

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

LLM_PORT="${LLM_PORT:-8080}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8090}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -x "$BASE_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$BASE_DIR/.venv/bin/python"
fi
export PYTHON_BIN

pid_matches() {
  local pid="$1"
  local script="$2"
  local args=""
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  case "$args" in
    *"$BASE_DIR/$script"*) return 0 ;;
    *) return 1 ;;
  esac
}

pid_file_running() {
  local pid_file="$1"
  local script="$2"
  local pid=""
  if [ -f "$pid_file" ]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
  fi
  pid_matches "$pid" "$script"
}

find_script_pid() {
  local script="$1"
  pgrep -af "$BASE_DIR/$script" 2>/dev/null | awk 'NR==1 {print $1}' || true
}

wait_http() {
  local name="$1"
  local url="$2"
  local log_file="$3"
  local elapsed=0
  while [ "$elapsed" -lt "$STARTUP_TIMEOUT" ]; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "$name OK: $url"
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  echo "ERRO: $name nao respondeu em ${STARTUP_TIMEOUT}s ($url)" >&2
  if [ -f "$log_file" ]; then
    echo "--- ultimas linhas de $log_file ---" >&2
    tail -n 40 "$log_file" >&2 || true
  fi
  return 1
}

start_python_service() {
  local name="$1"
  local script="$2"
  local pid_file="$3"
  local log_file="$4"
  local health_url="$5"

  if curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1; then
    echo "$name ja esta respondendo"
    return 0
  fi

  if pid_file_running "$pid_file" "$script"; then
    echo "$name ja esta rodando (PID $(cat "$pid_file"))"
  else
    local existing_pid
    existing_pid="$(find_script_pid "$script")"
    if [ -n "$existing_pid" ] && pid_matches "$existing_pid" "$script"; then
      echo "$existing_pid" > "$pid_file"
      echo "$name ja estava rodando (PID $existing_pid)"
    else
      : > "$log_file"
      nohup "$PYTHON_BIN" "$BASE_DIR/$script" > "$log_file" 2>&1 < /dev/null &
      echo $! > "$pid_file"
      echo "$name iniciado (PID $(cat "$pid_file"))"
    fi
  fi

  wait_http "$name" "$health_url" "$log_file"
}

start_paper_loop() {
  local loop_lock_pid=""
  if [ -f logs/paper_loop.lock ]; then
    loop_lock_pid="$(cat logs/paper_loop.lock 2>/dev/null || true)"
  fi

  if [ -n "$loop_lock_pid" ] && pid_matches "$loop_lock_pid" "paper_loop.sh"; then
    echo "paper_loop ja esta rodando (lock PID $loop_lock_pid)"
    return 0
  fi

  if pid_file_running "logs/paper_loop.pid" "paper_loop.sh"; then
    echo "paper_loop ja esta rodando (PID $(cat logs/paper_loop.pid))"
    return 0
  fi

  local existing_pid
  existing_pid="$(find_script_pid "paper_loop.sh")"
  if [ -n "$existing_pid" ] && pid_matches "$existing_pid" "paper_loop.sh"; then
    echo "$existing_pid" > logs/paper_loop.pid
    echo "paper_loop ja estava rodando (PID $existing_pid)"
    return 0
  fi

  : > logs/paper_loop.out
  nohup bash "$BASE_DIR/paper_loop.sh" > logs/paper_loop.out 2>&1 < /dev/null &
  echo $! > logs/paper_loop.pid
  echo "paper_loop iniciado (PID $(cat logs/paper_loop.pid))"

  sleep 1
  if ! pid_file_running "logs/paper_loop.pid" "paper_loop.sh"; then
    echo "ERRO: paper_loop encerrou logo apos iniciar" >&2
    tail -n 60 logs/paper_loop.out >&2 || true
    tail -n 60 logs/paper_runner.log >&2 || true
    exit 1
  fi
}

start_python_service \
  "LLM" \
  "llm_server_qwen.py" \
  "logs/llm_server.pid" \
  "logs/llm_server.log" \
  "http://${HEALTH_HOST}:${LLM_PORT}/health"

start_paper_loop

start_python_service \
  "Dashboard" \
  "monitor_web.py" \
  "logs/monitor_web.pid" \
  "logs/monitor_web.log" \
  "http://${HEALTH_HOST}:${DASHBOARD_PORT}/health"

echo
echo "Health checks:"
curl -fsS --max-time 6 "http://${HEALTH_HOST}:${LLM_PORT}/health" || true
echo
curl -fsS --max-time 6 "http://${HEALTH_HOST}:${DASHBOARD_PORT}/health" || true
echo

echo "Dashboard: http://${HEALTH_HOST}:${DASHBOARD_PORT}"
