# Polymarket Paper Trader + Qwen local (llama.cpp)

Projeto de paper trading para Polymarket com:
- scanner de estratégias,
- wallet simulada,
- loop contínuo de execução,
- seleção de entradas com LLM local pequeno (Qwen),
- dashboard web para acompanhar operação.

## Stack
- Python 3.10+
- `httpx`
- `llama-cpp-python`

## Estrutura
- `scanner.py` -> gera sinais
- `wallet.py` -> estado e risco da carteira paper
- `settlement.py` -> ciclo completo (settle + scan + entradas)
- `llm_server_qwen.py` -> endpoint local OpenAI-compat (`/v1/chat/completions`)
- `paper_loop.sh` -> roda `settlement.py full` a cada 5 min
- `monitor_web.py` -> dashboard web (`:8090`)

## Setup rápido
1) Entrar na pasta

```bash
cd polymarket-trader
```

2) Instalar dependências

```bash
python3 -m pip install -r requirements.txt
```

3) Configurar ambiente

```bash
cp .env.example .env
# ajuste valores se necessário
```

4) Subir tudo

```bash
./start_all.sh
```

5) Verificar

```bash
./status.sh
```

## URLs
- Dashboard: `http://127.0.0.1:8090`
- LLM health: `http://127.0.0.1:8080/health`

## Como funciona o papel do Qwen
- O Qwen é usado para **selecionar/rerankear sinais de entrada**.
- O fechamento de trades continua **determinístico** por regra de risco e mercado resolvido.
- Isso mantém robustez operacional em modelos pequenos.

## Camada OSINT (Plano B)
- O projeto pode enriquecer sinais com **Google News RSS** de forma determinística (sem LLM).
- Quando habilitado, cada sinal recebe `osint_news_hits`, `osint_score` e um pequeno `osint_bonus` no edge.
- Depois desse enriquecimento, o fluxo opcional com Qwen continua para rerank final.

### Flags OSINT
- `PAPER_OSINT_GOOGLE_NEWS_ENABLED` (1/0)
- `PAPER_OSINT_GOOGLE_NEWS_WINDOW_HOURS` (janela de recência)
- `PAPER_OSINT_GOOGLE_NEWS_MAX_ARTICLES` (cap de artigos por sinal)
- `PAPER_OSINT_GOOGLE_NEWS_BONUS_CAP` (bônus máximo adicionado ao edge)

## Comandos úteis
- Iniciar: `./start_all.sh`
- Status: `./status.sh`
- Parar: `./stop_all.sh`

## Observações
- Este projeto é paper trading (simulação), sem ordens reais.
- `wallet.json` e `logs/` não entram no git por padrão.
