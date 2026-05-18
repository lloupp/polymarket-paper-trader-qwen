# AGENTS.md

Objetivo: permitir que outro agente Hermes entenda e opere este projeto sem contexto prévio.

## O que este projeto faz
- Scanner de sinais paper para Polymarket (`scanner.py`)
- Carteira simulada e gestão de risco (`wallet.py`)
- Ciclo de execução com settlement/abertura (`settlement.py`)
- Seletor LLM local (Qwen via endpoint OpenAI-compat) para rerank de entradas (`llm_server_qwen.py` + flags PAPER_LLM_*)
- Loop contínuo com intervalo ajustável, 90s por padrão (`paper_loop.sh`)
- Runtime operacional com rotação/circuit breaker/relatórios (`ops_runtime.py`)
- Dashboard web de monitoramento (`monitor_web.py`)

## Regra de operação
1. Primeiro fecha posições por regras determinísticas (stop-loss/take-profit/mercado resolvido).
2. Depois escaneia sinais.
3. Depois (se habilitado) usa LLM para selecionar/reordenar entradas.
4. Abre operações em modo paper (simulado).

## Comandos principais
- Preparar máquina nova: `./bootstrap.sh`
- Subir tudo: `./start_all.sh`
- Ver status: `./status.sh`
- Parar tudo: `./stop_all.sh`
- Reiniciar só dashboard: `./restart_dashboard.sh`
- Reinstalar dependências: `./bootstrap.sh --force`

## Endpoints
- LLM local: `http://127.0.0.1:8080/v1/chat/completions`
- Health LLM: `http://127.0.0.1:8080/health`
- Dashboard: `http://127.0.0.1:8090`
- Health dashboard: `http://127.0.0.1:8090/health`

## Configuração por ambiente
Copiar `.env.example` para `.env` e ajustar:
- `PAPER_LLM_ENABLED` (1/0)
- `PAPER_LLM_SERVER_ENABLED` (1/0)
- `PAPER_LLM_MODE` (fast/balanced/strong)
- `PAPER_LLM_URL`
- `LLM_PORT`, `DASHBOARD_PORT`
- `DASHBOARD_HOST`, `DASHBOARD_TOKEN`
- `PAPER_LOOP_SECONDS`
- `PAPER_STRATEGY_MODE`
- `PAPER_MIN_NET_EDGE`, `PAPER_TAKER_FEE_ESTIMATE`, `PAPER_SLIPPAGE_ESTIMATE`
- `PAPER_SMART_MONEY_*`, `PAPER_EVENT_COUNTDOWN_*`, `PAPER_BTC_*`, `PAPER_ENDGAME_*`
- `PAPER_SHADOW_STRATEGIES`
- `PAPER_POLYMARKET_STATIC_DNS`
- `PAPER_WALLET_BACKUP_ENABLED`, `PAPER_WALLET_BACKUP_RETENTION`
- `PAPER_LEARNING_STORE_ENABLED`, `PAPER_LEARNING_OUTCOME_HORIZON_MINUTES`, `PAPER_LEARNING_PENDING_MAX`

## Logs
- `logs/paper_runner.log`
- `logs/last_report.txt`
- `logs/last_report.json`
- `logs/llm_server.log`
- `logs/monitor_web.log`
- `logs/learning_events.jsonl`
- `logs/learning_pending_signals.json`
- `logs/wallet_backups/`

## Startup/portabilidade
- `./start_all.sh` roda `./bootstrap.sh` automaticamente, salvo se `PAPER_SKIP_BOOTSTRAP=1`.
- `./bootstrap.sh` cria `.env`, `.venv`, instala `requirements.txt` e valida imports de `httpx`/`llama_cpp`.
- Health checks aguardam `STARTUP_TIMEOUT` segundos (45s por padrão) antes de falhar.
- Os scripts usam PID files e validam a command line para evitar falso positivo por PID reutilizado.
- Para rodar sem LLM, usar `PAPER_LLM_ENABLED=0` e `PAPER_LLM_SERVER_ENABLED=0`; nesse modo `status.sh` não deve tratar LLM desligado como erro.
- Para máquina com 8GB de RAM, começar com `PAPER_LLM_MODE=fast`; `balanced` tende a rodar, mas pode ficar lento; `strong` não é recomendado como padrão.
- Em máquina nova, se `llama-cpp-python` falhar, instalar ferramentas de build (`python3-venv`, `python3-dev`, `build-essential`, `cmake`) e rodar `./bootstrap.sh --force`.

## Learning
- `learning.py` aprende com trades paper fechados e ajusta política via `effective_min_edge`/multiplicadores.
- `learning_store.py` registra todos os sinais/decisões em `logs/learning_events.jsonl` e outcomes posteriores em modo contra-factual.
- `logs/learning_pending_signals.json` mantém sinais aguardando reobservação depois de `PAPER_LEARNING_OUTCOME_HORIZON_MINUTES`.

## Observações
- Não há execução real de ordens; é paper trading.
- Manter fechamento de posição determinístico é intencional para robustez.
- Sinais não executáveis diretamente em paper (ex.: arbitragem multi-leg `sell_all`) não devem ser convertidos para `YES`; devem ser ignorados até existir executor específico.
- Estratégias padrão recomendadas: `btc_5m_momentum,endgame_last_minute,smart_money,event_countdown`.
- `arbitrage,value,mean_reversion,volume_spike` ficam shadow-only por padrão e são configuráveis no dashboard.
- O dashboard controla `llm_enabled`, `llm_mode` e `llm_url` via `wallet.settings`; para o dashboard habilitar LLM de fato, o servidor Qwen precisa estar ligado por `PAPER_LLM_SERVER_ENABLED=1`.
