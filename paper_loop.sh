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

LOCK_FILE="${BASE_DIR}/logs/paper_loop.lock"
if [ -f "$LOCK_FILE" ]; then
  OLD_PID="$(cat "$LOCK_FILE" 2>/dev/null || true)"
  if [ -n "${OLD_PID}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[$(date -Is)] another paper_loop instance is already running (pid=${OLD_PID})" >> logs/paper_runner.log
    exit 0
  fi
fi
echo $$ > "$LOCK_FILE"
echo $$ > "${BASE_DIR}/logs/paper_loop.pid"
trap 'rm -f "$LOCK_FILE" "${BASE_DIR}/logs/paper_loop.pid"' EXIT

echo "[$(date -Is)] paper loop started" >> logs/paper_runner.log
STRATEGY_FILE="${BASE_DIR}/logs/active_strategy.txt"
LOOP_SECONDS_FILE="${BASE_DIR}/logs/loop_seconds.txt"
DEFAULT_MODE="${PAPER_STRATEGY_MODE:-btc_5m_momentum,endgame_last_minute,smart_money,event_countdown}"
DEFAULT_LOOP_SECONDS="${PAPER_LOOP_SECONDS:-90}"
if ! [[ "$DEFAULT_LOOP_SECONDS" =~ ^[0-9]+$ ]] || [ "$DEFAULT_LOOP_SECONDS" -lt 30 ]; then
  DEFAULT_LOOP_SECONDS=90
fi

resolve_loop_seconds() {
  local s="$DEFAULT_LOOP_SECONDS"
  if [ -f "$LOOP_SECONDS_FILE" ]; then
    local c
    c="$(tr -d '[:space:]' < "$LOOP_SECONDS_FILE")"
    if [[ "$c" =~ ^[0-9]+$ ]] && [ "$c" -ge 30 ] && [ "$c" -le 3600 ]; then
      s="$c"
    fi
  fi
  echo "$s"
}

PYBIN="${PYTHON_BIN:-python3}"
if [ -x "$BASE_DIR/.venv/bin/python" ]; then
  PYBIN="$BASE_DIR/.venv/bin/python"
fi
while true; do
  printf "\n===== %s =====\n" "$(date -Is)" | tee -a logs/paper_runner.log

  RUNTIME_JSON="$($PYBIN ops_runtime.py precycle 2>/dev/null || echo '{}')"
  MODE="$DEFAULT_MODE"
  if [ -f "$STRATEGY_FILE" ]; then
    CANDIDATE="$(tr -d '[:space:]' < "$STRATEGY_FILE")"
    if [ -n "$CANDIDATE" ]; then MODE="$CANDIDATE"; fi
  fi

  DISABLE_ENTRIES=0
  if echo "$RUNTIME_JSON" | grep -q '"entries_paused": true'; then
    DISABLE_ENTRIES=1
  fi

  if out=$(PAPER_STRATEGY_MODE="$MODE" PAPER_DISABLE_NEW_ENTRIES="$DISABLE_ENTRIES" "$PYBIN" settlement.py full 2>&1); then
    printf "%s\n" "$out" | tee -a logs/paper_runner.log >/dev/null
    printf "%s\n" "$out" > logs/last_report.txt
  else
    printf "%s\n" "$out" | tee -a logs/paper_runner.log >/dev/null
  fi

  $PYBIN ops_runtime.py postcycle >/dev/null 2>&1 || true

  LOOP_SECONDS="$(resolve_loop_seconds)"
  sleep "$LOOP_SECONDS"
done
