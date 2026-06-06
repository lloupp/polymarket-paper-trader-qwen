# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Comandos principais

```bash
# Preparar ambiente (cria .venv, instala deps, cria .env se não existir)
./bootstrap.sh
./bootstrap.sh --force   # reinstala dependências do zero

# Ciclo de vida dos serviços
./start_all.sh           # sobe LLM (se habilitado), loop paper e dashboard
./stop_all.sh            # encerra todos os serviços
./status.sh              # mostra processos vivos e health checks
./restart_dashboard.sh   # reinicia só o dashboard

# Testes
.venv/bin/pytest                        # todos os testes
.venv/bin/pytest tests/test_wallet.py   # arquivo específico

# Lint
.venv/bin/ruff check .
.venv/bin/ruff check --fix .
```

`start_all.sh` chama `bootstrap.sh` automaticamente (pule com `PAPER_SKIP_BOOTSTRAP=1`).

## Arquitetura

O sistema é um paper trader para Polymarket com ciclo de ~30–90 s. Cada componente é um módulo Python standalone, sem framework web além do dashboard.

### Fluxo de um ciclo

```
paper_loop.sh
  ├── ops_runtime.py precycle   → decide se pausar entradas (circuit breaker)
  ├── settlement.py full
  │     ├── wallet.py            → lê/escreve wallet.json (lock fcntl)
  │     ├── scanner.py           → busca Polymarket Gamma API, retorna sinais
  │     ├── learning.py          → ajusta effective_min_edge e multiplicadores
  │     ├── learning_store.py    → grava eventos em logs/learning_events.jsonl
  │     └── llm_server_qwen.py  → rerank opcional via HTTP (OpenAI-compat)
  └── ops_runtime.py postcycle  → atualiza timeline, rotação de estratégia
```

### Módulos

| Arquivo | Responsabilidade |
|---|---|
| `settlement.py` | Orchestration: fecha posições (stop/TP/resolved), escaneia, filtra, executa paper |
| `scanner.py` | Fetch assíncrono da Gamma API + 8 estratégias de sinal |
| `wallet.py` | CRUD de posições no `wallet.json`; lock de arquivo; trailing stop/TP |
| `learning.py` | Aprende com trades fechados; ajusta `effective_min_edge` e `strategy_multipliers` |
| `learning_store.py` | Append-only log de sinais/decisões e outcomes contra-factuais |
| `ops_runtime.py` | Circuit breaker, rotação de estratégia, timeline, relatórios |
| `monitor_web.py` | Dashboard HTTP na porta 8090; lê `wallet.json` e logs |
| `llm_server_qwen.py` | Servidor HTTP mínimo que expõe Qwen (llama-cpp) em `/v1/chat/completions` |

### Estado persistido

- `wallet.json` — fonte de verdade única: bankroll, posições abertas, histórico, cooldowns, settings, learning_state
- `logs/learning_events.jsonl` — append-only; nunca reescrever
- `logs/last_report.json` — resultado estruturado do último ciclo
- `logs/active_strategy.txt` e `logs/loop_seconds.txt` — overrides de runtime que o dashboard escreve

### Estratégias

Estratégias em `PAPER_STRATEGY_MODE` executam entradas paper. As em `PAPER_SHADOW_STRATEGIES` geram sinais nos relatórios, mas **não viram posições**.

Padrão recomendado:
```
PAPER_STRATEGY_MODE=btc_5m_momentum,endgame_last_minute,smart_money,event_countdown,weather_forecast
PAPER_SHADOW_STRATEGIES=arbitrage,value,mean_reversion,volume_spike
```

Sinais `sell_all` (arbitragem multi-leg) não devem ser convertidos para `YES`/`NO` — ignorar até existir executor específico.

### LLM

`llm_server_qwen.py` é um servidor HTTP mínimo (`ThreadingHTTPServer`). O modelo GGUF é baixado do Hugging Face na primeira chamada, não no boot. O `settlement.py` chama o endpoint com `mode=fast|balanced|strong` no body da requisição (extensão não-padrão do OpenAI schema).

Para habilitar LLM via dashboard, `PAPER_LLM_SERVER_ENABLED=1` no `.env` é obrigatório — o dashboard só alterna o flag `wallet.settings.llm_enabled`, não sobe o processo.

## Convenções de código

- `wallet.json` é sempre gravado com `.tmp` + rename atômico (`save_json` em `ops_runtime.py`).
- O lock de `wallet.json` usa `fcntl.flock` (`wallet.py:Wallet`).
- Todos os módulos usam `datetime.now(timezone.utc)` — nunca `datetime.now()` sem timezone.
- Testes importam direto da raiz via `sys.path.insert(0, ROOT)`; não há pacote instalável.
- Linha máxima: 120. Ruff checks: `E, F, I, UP, B` (E501 ignorado).
