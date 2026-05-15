# AGENTS.md

Objetivo: permitir que outro agente Hermes entenda e opere este projeto sem contexto prévio.

## O que este projeto faz
- Scanner de sinais paper para Polymarket (`scanner.py`)
- Carteira simulada e gestão de risco (`wallet.py`)
- Ciclo de execução com settlement/abertura (`settlement.py`)
- Seletor LLM local (Qwen via endpoint OpenAI-compat) para rerank de entradas (`llm_server_qwen.py` + flags PAPER_LLM_*)
- Loop contínuo a cada 5 minutos (`paper_loop.sh`)
- Dashboard web de monitoramento (`monitor_web.py`)

## Regra de operação
1. Primeiro fecha posições por regras determinísticas (stop-loss/take-profit/mercado resolvido).
2. Depois escaneia sinais.
3. Depois (se habilitado) usa LLM para selecionar/reordenar entradas.
4. Abre operações em modo paper (simulado).

## Comandos principais
- Subir tudo: `./start_all.sh`
- Ver status: `./status.sh`
- Parar tudo: `./stop_all.sh`

## Endpoints
- LLM local: `http://127.0.0.1:8080/v1/chat/completions`
- Health LLM: `http://127.0.0.1:8080/health`
- Dashboard: `http://127.0.0.1:8090`
- Health dashboard: `http://127.0.0.1:8090/health`

## Configuração por ambiente
Copiar `.env.example` para `.env` e ajustar:
- `PAPER_LLM_ENABLED` (1/0)
- `PAPER_LLM_MODE` (fast/balanced)
- `PAPER_LLM_URL`
- `LLM_PORT`, `DASHBOARD_PORT`

## Logs
- `logs/paper_runner.log`
- `logs/last_report.txt`
- `logs/llm_server.log`
- `logs/monitor_web.log`

## Observações
- Não há execução real de ordens; é paper trading.
- Manter fechamento de posição determinístico é intencional para robustez.
