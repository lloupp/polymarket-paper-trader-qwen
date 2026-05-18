# Polymarket Paper Trader

Bot de paper trading para Polymarket com scanner de sinais, carteira simulada, loop contínuo de execução, dashboard web e rerank opcional com LLM local Qwen.

Não executa ordens reais. Tudo é simulado em `wallet.json`.

## Como funciona
1. Fecha posições abertas por regras determinísticas: stop-loss, take-profit ou mercado resolvido.
2. Escaneia novos sinais de mercado.
3. Aplica filtros de execução: edge líquido, spread, liquidez, volume, preço de entrada e shadow strategies.
4. Se o LLM estiver habilitado, usa Qwen local para selecionar/reordenar entradas.
5. Abre novas posições paper na carteira simulada.

O ciclo roda por `paper_loop.sh`. O intervalo padrão é `90s` e pode ser alterado no `.env` ou no dashboard.

## Requisitos
- Linux, WSL ou ambiente equivalente com Bash.
- Python 3.10+.
- Internet para buscar dados da Polymarket e, se usar LLM, baixar o modelo GGUF na primeira chamada.
- Para instalar `llama-cpp-python`, pode ser necessário ter ferramentas de build.

Em Ubuntu/WSL, se a instalação falhar:

```bash
sudo apt update
sudo apt install -y python3-venv python3-dev build-essential cmake
./bootstrap.sh --force
```

## Primeira vez
1. Clone o repositório e entre na pasta.

```bash
git clone https://github.com/lloupp/polymarket-paper-trader-qwen.git
cd polymarket-paper-trader-qwen
```

2. Prepare o ambiente local.

```bash
./bootstrap.sh
```

O bootstrap cria `.env` a partir de `.env.example`, cria `.venv`, instala dependências e valida imports básicos.

3. Revise a configuração.

```bash
nano .env
```

4. Suba os serviços.

```bash
./start_all.sh
```

5. Verifique o status.

```bash
./status.sh
```

6. Abra o dashboard.

```text
http://127.0.0.1:8090
```

## Rodar sem LLM
Este é o modo mais leve e recomendado para validar a instalação em qualquer máquina.

No `.env`, deixe:

```bash
PAPER_LLM_ENABLED=0
PAPER_LLM_SERVER_ENABLED=0
```

Depois rode:

```bash
./start_all.sh
```

Nesse modo, o bot usa apenas regras determinísticas, scoring local e filtros de execução. O dashboard, scanner, carteira, learning, OSINT opcional e circuit breaker continuam funcionando.

## Rodar com Qwen local
Para usar o Qwen, habilite o servidor local e o rerank.

No `.env`:

```bash
PAPER_LLM_ENABLED=1
PAPER_LLM_SERVER_ENABLED=1
PAPER_LLM_MODE=fast
PAPER_LLM_URL=http://127.0.0.1:8080/v1/chat/completions
```

Depois reinicie:

```bash
./stop_all.sh
./start_all.sh
```

O primeiro uso do LLM pode demorar porque o `llama-cpp-python` baixa o GGUF do Hugging Face. O endpoint sobe antes de carregar modelo; o modelo é carregado sob demanda na primeira chamada.

## Qwen em máquina com 8GB RAM
Funciona, mas use o modo certo.

- `fast`: Qwen2.5-0.5B quantizado. É o mais indicado para 8GB RAM e máquinas comuns.
- `balanced`: Qwen2.5-1.5B quantizado. Deve rodar em 8GB se a máquina não estiver muito carregada, mas pode ficar lento.
- `strong`: Qwen3-4B quantizado. Não é recomendado como padrão em 8GB; use só se houver folga de RAM/CPU.

Para outro computador com 8GB, comece com:

```bash
PAPER_LLM_MODE=fast
```

Se ficar estável, teste:

```bash
PAPER_LLM_MODE=balanced
```

Se não quiser usar LLM, deixe `PAPER_LLM_ENABLED=0` e `PAPER_LLM_SERVER_ENABLED=0`.

## Continuar depois
Se o computador reiniciou ou você fechou o terminal:

```bash
cd polymarket-paper-trader-qwen
./start_all.sh
./status.sh
```

Para parar:

```bash
./stop_all.sh
```

Para reiniciar apenas o dashboard:

```bash
./restart_dashboard.sh
```

## Atualizar código depois
Se houver mudanças no repositório remoto:

```bash
cd polymarket-paper-trader-qwen
./stop_all.sh
git pull
./bootstrap.sh
./start_all.sh
```

`wallet.json`, `.env` e `logs/` são locais e não entram no Git por padrão.

## Comandos principais
- `./bootstrap.sh`: cria/atualiza `.venv`, instala dependências e cria `.env` se não existir.
- `./bootstrap.sh --force`: reinstala dependências.
- `./start_all.sh`: sobe LLM se habilitado, loop paper e dashboard.
- `./status.sh`: mostra processos e health checks.
- `./stop_all.sh`: encerra serviços do projeto.
- `./restart_dashboard.sh`: reinicia só o dashboard.

## Endpoints
- Dashboard: `http://127.0.0.1:8090`
- Health dashboard: `http://127.0.0.1:8090/health`
- LLM local, quando habilitado: `http://127.0.0.1:8080/v1/chat/completions`
- Health LLM, quando habilitado: `http://127.0.0.1:8080/health`

## Configuração principal
Variáveis mais importantes do `.env`:

- `PAPER_LLM_ENABLED`: ativa/desativa uso do LLM no rerank de sinais.
- `PAPER_LLM_SERVER_ENABLED`: sobe ou não o servidor Qwen local.
- `PAPER_LLM_MODE`: `fast`, `balanced` ou `strong`.
- `PAPER_LLM_URL`: endpoint OpenAI-compatível do LLM.
- `LLM_PORT`: porta do servidor LLM.
- `DASHBOARD_HOST`: host do dashboard, padrão `127.0.0.1`.
- `DASHBOARD_PORT`: porta do dashboard, padrão `8090`.
- `DASHBOARD_TOKEN`: token opcional para proteger dashboard/API.
- `PAPER_LOOP_SECONDS`: intervalo do loop.
- `PAPER_STRATEGY_MODE`: estratégias executáveis.
- `PAPER_SHADOW_STRATEGIES`: estratégias que geram sinal, mas não executam.
- `PAPER_MIN_NET_EDGE`: edge líquido mínimo para entrada.
- `PAPER_TAKER_FEE_ESTIMATE`: estimativa de fee.
- `PAPER_SLIPPAGE_ESTIMATE`: estimativa de slippage.
- `PAPER_POLYMARKET_STATIC_DNS`: fallback DNS estático, desligado por padrão.
- `PAPER_WALLET_BACKUP_ENABLED`: backup local da carteira antes de cada ciclo.
- `PAPER_OSINT_GOOGLE_NEWS_ENABLED`: enriquecimento OSINT via Google News RSS.

## Estratégias
Modo recomendado:

```bash
PAPER_STRATEGY_MODE=btc_5m_momentum,endgame_last_minute,smart_money,event_countdown
```

Shadow padrão:

```bash
PAPER_SHADOW_STRATEGIES=arbitrage,value,mean_reversion,volume_spike
```

Estratégias em shadow aparecem nos relatórios, mas não viram entradas paper até serem removidas de `PAPER_SHADOW_STRATEGIES` ou alteradas pelo dashboard.

## Dashboard
O dashboard permite:

- Ver bankroll, P&L, posições abertas e relatório do último ciclo.
- Ligar/desligar LLM no `wallet.settings`.
- Escolher `fast`, `balanced` ou `strong`.
- Ajustar risco, sizing, stop-loss, take-profit e filtros de execução.
- Alterar estratégias ativas e intervalo do loop.
- Rodar um ciclo manual.
- Ver timeline operacional, learning e notícias OSINT.

Importante: para habilitar LLM pelo dashboard, o servidor Qwen precisa estar rodando. Se `PAPER_LLM_SERVER_ENABLED=0`, altere para `1` no `.env` e reinicie com `./stop_all.sh && ./start_all.sh`.

## Logs e estado local
- `wallet.json`: carteira simulada local.
- `logs/paper_runner.log`: log principal do loop.
- `logs/last_report.txt`: último relatório em texto.
- `logs/last_report.json`: último relatório estruturado.
- `logs/llm_server.log`: log do servidor Qwen.
- `logs/monitor_web.log`: log do dashboard.
- `logs/wallet_backups/`: backups locais da wallet.

## Troubleshooting
Se o dashboard não abre:

```bash
./status.sh
tail -n 80 logs/monitor_web.log
```

Se o loop não roda:

```bash
./status.sh
tail -n 120 logs/paper_runner.log
```

Se o LLM não responde:

```bash
./status.sh
tail -n 120 logs/llm_server.log
```

Se você não quer usar LLM, isso não é erro. Confirme que está assim:

```bash
PAPER_LLM_ENABLED=0
PAPER_LLM_SERVER_ENABLED=0
```

Se aparecer erro de DNS para Polymarket, primeiro confirme internet/DNS da máquina. O fallback estático existe, mas deve ser usado só como contingência:

```bash
PAPER_POLYMARKET_STATIC_DNS=1
```

## Arquivos versionados e locais
Entram no Git:

- Código Python.
- Scripts `.sh`.
- `README.md`, `AGENTS.md`, `.env.example`.
- Testes em `tests/`.

Não entram no Git:

- `.env`
- `.venv/`
- `wallet.json`
- `logs/`
