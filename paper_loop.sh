#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
mkdir -p logs

echo "[$(date -Is)] paper loop started" >> logs/paper_runner.log
while true; do
  echo "\n===== $(date -Is) =====" | tee -a logs/paper_runner.log
  if out=$(PAPER_LLM_ENABLED=1 PAPER_LLM_MODE=fast python3 settlement.py full 2>&1); then
    printf "%s\n" "$out" | tee -a logs/paper_runner.log >/dev/null
    printf "%s\n" "$out" > logs/last_report.txt
  else
    printf "%s\n" "$out" | tee -a logs/paper_runner.log >/dev/null
  fi
  sleep 300
done
