#!/usr/bin/env bash
set -euo pipefail

pkill -f "python3 .*monitor_web.py" || true
pkill -f "bash .*paper_loop.sh" || true
pkill -f "python3 .*llm_server_qwen.py" || true

echo "Serviços encerrados (se estavam ativos)."
