# Polymarket Paper Trader + Qwen local (llama.cpp)

Projeto de paper trading para Polymarket com:
- scanner de estratégias,
- wallet simulada,
- loop contínuo de execução,
- seleção de entradas com LLM local pequeno (Qwen),
- dashboard web para acompanhar operação.

## Resumo único ("tudo junto")
1. O sistema **fecha posições primeiro** por regras determinísticas (stop-loss/take-profit/mercado resolvido).
2. Depois, roda o scanner para gerar sinais.
3. Em seguida, se habilitado, usa o Qwen local para selecionar/reordenar entradas.
4. Por fim, abre novas operações em modo paper.

Esse ciclo roda continuamente via `paper_loop.sh`; o padrão atual é 90s e pode ser ajustado por `PAPER_LOOP_SECONDS` ou pelo dashboard.

## Stack
- Python 3.10+
- `httpx`
- `llama-cpp-python`

## Estrutura
- `scanner.py` -> gera sinais
- `wallet.py` -> estado e risco da carteira paper
- `settlement.py` -> ciclo completo (settle + scan + entradas)
- `llm_server_qwen.py` -> endpoint local OpenAI-compat (`/v1/chat/completions`)
- `paper_loop.sh` -> roda `settlement.py full` em loop contínuo (90s por padrão)
- `ops_runtime.py` -> rotação/circuit breaker/relatórios operacionais
- `monitor_web.py` -> dashboard web (`:8090`)

## Setup rápido
1) Entrar na pasta

```bash
cd polymarket-paper-trader-qwen
```

2) Preparar ambiente local

```bash
./bootstrap.sh
```

Esse comando cria `.env` a partir de `.env.example`, cria `.venv`, instala dependências e valida imports básicos.

3) Configurar ambiente, se quiser alterar defaults

```bash
nano .env
```

4) Subir tudo

```bash
./start_all.sh
```

5) Verificar

```bash
./status.sh
```

## Comandos principais
- Subir tudo: `./start_all.sh`
- Ver status: `./status.sh`
- Parar tudo: `./stop_all.sh`
- Reiniciar só dashboard: `./restart_dashboard.sh`
- Reinstalar dependências: `./bootstrap.sh --force`

`./start_all.sh` também roda o bootstrap automaticamente quando necessário, espera os health checks responderem e mostra as últimas linhas de log se algum serviço não subir.

## Endpoints
- LLM local: `http://127.0.0.1:8080/v1/chat/completions`
- Health LLM: `http://127.0.0.1:8080/health`
- Dashboard: `http://127.0.0.1:8090`
- Health dashboard: `http://127.0.0.1:8090/health`

## Configuração por ambiente
Ajuste no `.env`:
- `PAPER_LLM_ENABLED` (1/0)
- `PAPER_LLM_MODE` (fast/balanced/strong)
- `PAPER_LLM_URL`
- `LLM_PORT`, `DASHBOARD_HOST`, `DASHBOARD_PORT`
- `DASHBOARD_TOKEN` (opcional; se definido, acesse `/?token=...`)
- `PAPER_LOOP_SECONDS`
- `PAPER_STRATEGY_MODE` (default recomendado: `btc_5m_momentum,endgame_last_minute,smart_money,event_countdown`)
- `PAPER_MIN_NET_EDGE`, `PAPER_TAKER_FEE_ESTIMATE`, `PAPER_SLIPPAGE_ESTIMATE`
- `PAPER_SMART_MONEY_*`, `PAPER_EVENT_COUNTDOWN_*`, `PAPER_BTC_*`, `PAPER_ENDGAME_*`
- `PAPER_SHADOW_STRATEGIES` (default: `arbitrage,value,mean_reversion,volume_spike`)
- `PAPER_POLYMARKET_STATIC_DNS` (fallback DNS, desligado por padrão)
- `PAPER_WALLET_BACKUP_ENABLED`, `PAPER_WALLET_BACKUP_RETENTION`

## Logs
- `logs/paper_runner.log`
- `logs/last_report.txt`
- `logs/last_report.json`
- `logs/llm_server.log`
- `logs/monitor_web.log`
- `logs/wallet_backups/`

## Levar para outro computador
1. Clone o repositório.
2. Rode `./bootstrap.sh`.
3. Ajuste `.env` se necessário.
4. Rode `./start_all.sh`.
5. Abra `http://127.0.0.1:8090`.

Se o install do `llama-cpp-python` falhar, instale as ferramentas de build do sistema e rode `./bootstrap.sh --force`. Em Ubuntu/WSL, normalmente: `sudo apt update && sudo apt install -y python3-venv python3-dev build-essential cmake`.

## Como funciona o papel do Qwen
- O Qwen é usado para **selecionar/rerankear sinais de entrada**.
- O modo `fast` usa Qwen2.5-0.5B, `balanced` usa Qwen2.5-1.5B, e `strong` usa Qwen3-4B-Instruct-2507 em GGUF.
- O padrão recomendado é `balanced`; use `fast` se a máquina tiver pouca memória/CPU.
- O fechamento de trades continua **determinístico** por regra de risco e mercado resolvido.
- Isso mantém robustez operacional em modelos pequenos.
- O loop respeita `llm_enabled`, `llm_mode` e `llm_url` salvos pelo dashboard em `wallet.settings`.

## Política de estratégias
- Modo recomendado: `btc_5m_momentum,endgame_last_minute,smart_money,event_countdown`.
- `arbitrage`, `value`, `mean_reversion` e `volume_spike` ficam em shadow por padrão; elas podem gerar sinais, mas não executam até serem removidas de `shadow_strategies`.
- Antes de executar, o bot calcula `net_edge = edge - spread - fee_estimate - slippage_estimate` e exige `net_edge >= min_net_edge`.
- Os limites de spread/liquidez/volume/preço de entrada podem ser ajustados pelo dashboard em "Ajustes da operação".

## Camada OSINT (Plano B)
- O projeto pode enriquecer sinais com **Google News RSS** de forma determinística (sem LLM).
- Quando habilitado, cada sinal recebe `osint_news_hits`, `osint_score` e um pequeno `osint_bonus` no edge.
- Depois desse enriquecimento, o fluxo opcional com Qwen continua para rerank final.

### Flags OSINT
- `PAPER_OSINT_GOOGLE_NEWS_ENABLED` (1/0)
- `PAPER_OSINT_GOOGLE_NEWS_WINDOW_HOURS` (janela de recência)
- `PAPER_OSINT_GOOGLE_NEWS_MAX_ARTICLES` (cap de artigos por sinal)
- `PAPER_OSINT_GOOGLE_NEWS_BONUS_CAP` (bônus máximo adicionado ao edge)

## Observações
- Este projeto é paper trading (simulação), sem ordens reais.
- `wallet.json` e `logs/` não entram no git por padrão.
- Sinais não executáveis diretamente em paper, como arbitragem multi-leg `sell_all`, são ignorados pelo executor até existir implementação específica.
