#!/usr/bin/env bash
set -euo pipefail

echo "=== processos ==="
pgrep -af "llm_server_qwen.py|paper_loop.sh|monitor_web.py" || true

echo "\n=== health LLM ==="
curl -sS --max-time 6 http://127.0.0.1:8080/health || true

echo "\n=== health dashboard ==="
curl -sS --max-time 6 http://127.0.0.1:8090/health || true

echo
