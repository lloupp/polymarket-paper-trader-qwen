#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$BASE_DIR"
mkdir -p logs

PYTHON_BOOTSTRAP="${PYTHON_BOOTSTRAP:-python3}"
FORCE_INSTALL=0
if [ "${1:-}" = "--force" ]; then
  FORCE_INSTALL=1
fi

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo ".env criado a partir de .env.example"
  else
    echo "ERRO: .env.example nao encontrado" >&2
    exit 1
  fi
fi

if [ ! -d .venv ]; then
  if ! command -v "$PYTHON_BOOTSTRAP" >/dev/null 2>&1; then
    echo "ERRO: Python nao encontrado (${PYTHON_BOOTSTRAP}). Instale Python 3.10+." >&2
    exit 1
  fi
  "$PYTHON_BOOTSTRAP" -m venv .venv
  echo "venv criada em .venv"
fi

PYTHON_BIN="$BASE_DIR/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "ERRO: Python da venv nao encontrado em $PYTHON_BIN" >&2
  exit 1
fi

REQ_HASH=""
if command -v sha256sum >/dev/null 2>&1; then
  REQ_HASH="$(sha256sum requirements.txt | awk '{print $1}')"
else
  REQ_HASH="$(wc -c requirements.txt | awk '{print $1}')"
fi

MARKER="logs/requirements.sha256"
CURRENT_HASH=""
if [ -f "$MARKER" ]; then
  CURRENT_HASH="$(cat "$MARKER" 2>/dev/null || true)"
fi

deps_import_ok() {
  "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import httpx
import llama_cpp
PY
}

if [ "$FORCE_INSTALL" = "1" ] || [ "$REQ_HASH" != "$CURRENT_HASH" ]; then
  if [ "$FORCE_INSTALL" != "1" ] && deps_import_ok; then
    printf "%s" "$REQ_HASH" > "$MARKER"
    echo "dependencias ja estavam disponiveis"
  else
    "$PYTHON_BIN" -m pip install --upgrade pip
    "$PYTHON_BIN" -m pip install --prefer-binary -r requirements.txt
    printf "%s" "$REQ_HASH" > "$MARKER"
    echo "dependencias instaladas/atualizadas"
  fi
else
  echo "dependencias ja estao atualizadas"
fi

if ! "$PYTHON_BIN" - <<'PY'
import httpx
import llama_cpp
print("imports ok: httpx, llama_cpp")
PY
then
  echo "ERRO: imports de dependencias falharam. Rode ./bootstrap.sh --force." >&2
  exit 1
fi

echo "bootstrap concluido"
