"""
Standalone settlement script — checks open positions against live market
data and closes positions that hit stop-loss, take-profit, or market resolution.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import httpx

from common import (
    KNOWN_STRATEGIES,
    RECOMMENDED_MODE as RECOMMENDED_STRATEGY_MODE,
    install_polymarket_dns_fallback,
    yes_no_from_gamma_market,
)
from learning import ensure_learning_state, learning_snapshot, maybe_refresh_policy
from learning_store import (
    new_cycle_id,
    observe_signal_outcomes_from_signals,
    record_signal_decisions,
    summarize_learning_events,
)
from wallet import Wallet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("polymarket-settlement")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

install_polymarket_dns_fallback()

LLM_PAPER_ENABLED = os.getenv("PAPER_LLM_ENABLED", "0") == "1"
LLM_PAPER_URL = os.getenv("PAPER_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_PAPER_MODE = os.getenv("PAPER_LLM_MODE", "fast")
PAPER_DISABLE_NEW_ENTRIES = os.getenv("PAPER_DISABLE_NEW_ENTRIES", "0") == "1"

OSINT_GOOGLE_NEWS_ENABLED = os.getenv("PAPER_OSINT_GOOGLE_NEWS_ENABLED", "0") == "1"
OSINT_GOOGLE_NEWS_WINDOW_HOURS = int(os.getenv("PAPER_OSINT_GOOGLE_NEWS_WINDOW_HOURS", "24"))
OSINT_GOOGLE_NEWS_MAX_ARTICLES = int(os.getenv("PAPER_OSINT_GOOGLE_NEWS_MAX_ARTICLES", "8"))
OSINT_GOOGLE_NEWS_BONUS_CAP = float(os.getenv("PAPER_OSINT_GOOGLE_NEWS_BONUS_CAP", "0.03"))
WALLET_BACKUP_ENABLED = os.getenv("PAPER_WALLET_BACKUP_ENABLED", "1") == "1"
WALLET_BACKUP_RETENTION = int(os.getenv("PAPER_WALLET_BACKUP_RETENTION", "48"))

# Accepts: 'all', single strategy, or comma-separated strategies.
PAPER_STRATEGY_MODE = os.getenv("PAPER_STRATEGY_MODE", RECOMMENDED_STRATEGY_MODE).strip().lower()
PAPER_MIN_NET_EDGE = float(os.getenv("PAPER_MIN_NET_EDGE", "0.035"))
PAPER_TAKER_FEE_ESTIMATE = float(os.getenv("PAPER_TAKER_FEE_ESTIMATE", "0.001"))
PAPER_SLIPPAGE_ESTIMATE = float(os.getenv("PAPER_SLIPPAGE_ESTIMATE", "0.01"))
PAPER_SMART_MONEY_MAX_SPREAD = float(os.getenv("PAPER_SMART_MONEY_MAX_SPREAD", "0.03"))
PAPER_SMART_MONEY_MIN_LIQUIDITY = float(os.getenv("PAPER_SMART_MONEY_MIN_LIQUIDITY", "15000"))
PAPER_SMART_MONEY_MIN_VOL24H = float(os.getenv("PAPER_SMART_MONEY_MIN_VOL24H", "50000"))
PAPER_EVENT_COUNTDOWN_MAX_SPREAD = float(os.getenv("PAPER_EVENT_COUNTDOWN_MAX_SPREAD", "0.06"))
PAPER_EVENT_COUNTDOWN_MIN_LIQUIDITY = float(os.getenv("PAPER_EVENT_COUNTDOWN_MIN_LIQUIDITY_EXEC", "15000"))
PAPER_EVENT_COUNTDOWN_MIN_VOL24H = float(os.getenv("PAPER_EVENT_COUNTDOWN_MIN_VOL24H_EXEC", "25000"))
PAPER_BTC_MAX_ENTRY_PRICE_EXEC = float(os.getenv("PAPER_BTC_MAX_ENTRY_PRICE_EXEC", "0.82"))
PAPER_BTC_MIN_LIQUIDITY_EXEC = float(os.getenv("PAPER_BTC_MIN_LIQUIDITY_EXEC", "1000"))
PAPER_ENDGAME_MAX_ENTRY_PRICE_EXEC = float(os.getenv("PAPER_ENDGAME_MAX_ENTRY_PRICE_EXEC", "0.90"))
PAPER_ENDGAME_MIN_LIQUIDITY_EXEC = float(os.getenv("PAPER_ENDGAME_MIN_LIQUIDITY_EXEC", "1500"))
PAPER_SHADOW_STRATEGIES = os.getenv("PAPER_SHADOW_STRATEGIES", "arbitrage,value,mean_reversion,volume_spike,weather_forecast")
GAMMA_TIMEOUT_SECONDS = float(os.getenv("PAPER_GAMMA_TIMEOUT_SECONDS", "8"))
GAMMA_RETRY_ATTEMPTS = int(os.getenv("PAPER_GAMMA_RETRY_ATTEMPTS", "2"))
BTC_ALIASES = {"btc_5m", "btc_5m_momentum", "ndjjwobaq"}

DEFAULT_EXECUTION_POLICY = {
    "min_net_edge": PAPER_MIN_NET_EDGE,
    "taker_fee_estimate": PAPER_TAKER_FEE_ESTIMATE,
    "slippage_estimate": PAPER_SLIPPAGE_ESTIMATE,
    "smart_money_max_spread": PAPER_SMART_MONEY_MAX_SPREAD,
    "smart_money_min_liquidity": PAPER_SMART_MONEY_MIN_LIQUIDITY,
    "smart_money_min_vol24h": PAPER_SMART_MONEY_MIN_VOL24H,
    "event_countdown_max_spread": PAPER_EVENT_COUNTDOWN_MAX_SPREAD,
    "event_countdown_min_liquidity": PAPER_EVENT_COUNTDOWN_MIN_LIQUIDITY,
    "event_countdown_min_vol24h": PAPER_EVENT_COUNTDOWN_MIN_VOL24H,
    "btc_max_entry_price": PAPER_BTC_MAX_ENTRY_PRICE_EXEC,
    "btc_min_liquidity": PAPER_BTC_MIN_LIQUIDITY_EXEC,
    "endgame_max_entry_price": PAPER_ENDGAME_MAX_ENTRY_PRICE_EXEC,
    "endgame_min_liquidity": PAPER_ENDGAME_MIN_LIQUIDITY_EXEC,
    "shadow_strategies": PAPER_SHADOW_STRATEGIES,
}


def _selected_strategies(raw: str) -> set[str]:
    v = (raw or '').strip().lower()
    if not v or v == 'all':
        return set(KNOWN_STRATEGIES)
    parts = [p.strip() for p in v.replace(';', ',').split(',') if p.strip()]
    selected = set()
    for p in parts:
        if p in BTC_ALIASES:
            selected.add('btc_5m_momentum')
        elif p in KNOWN_STRATEGIES:
            selected.add(p)
    return selected or {'btc_5m_momentum'}


def execution_policy_from_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    policy = dict(DEFAULT_EXECUTION_POLICY)
    for key, default in DEFAULT_EXECUTION_POLICY.items():
        if key not in settings:
            continue
        if key == "shadow_strategies":
            policy[key] = str(settings.get(key) or "")
        else:
            try:
                policy[key] = float(settings.get(key, default))
            except (TypeError, ValueError):
                policy[key] = default
    return policy

def _json_list(value: Any) -> List[Any]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return value if isinstance(value, list) else []


def _float_or_none(value: Any) -> Optional[float]:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= price <= 1:
        return price
    return None


def _side_index(side: str) -> int:
    return 0 if str(side).upper() == "YES" else 1


def resolved_side_prices_from_gamma(data: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    prices = [_float_or_none(v) for v in _json_list(data.get("outcomePrices", ""))]
    if len(prices) < 2 or prices[0] is None or prices[1] is None:
        return None
    yes_price = float(prices[0])
    no_price = float(prices[1])
    # Resolution payout should be a near-binary outcome. Avoid using stale
    # probability snapshots as a deterministic settlement price.
    if yes_price in {0.0, 1.0} and no_price in {0.0, 1.0}:
        return yes_price, no_price
    return None


def _best_book_price(levels: Any, *, best: str) -> Optional[float]:
    if not isinstance(levels, list):
        return None
    prices = [_float_or_none(item.get("price") if isinstance(item, dict) else None) for item in levels]
    prices = [p for p in prices if p is not None and 0 < p < 1]
    if not prices:
        return None
    return min(prices) if best == "ask" else max(prices)


async def fetch_clob_book_quote(client: httpx.AsyncClient, token_id: str) -> Dict[str, Optional[float]]:
    for attempt in range(GAMMA_RETRY_ATTEMPTS):
        try:
            resp = await client.get(f"{CLOB_API}/book", params={"token_id": token_id})
            resp.raise_for_status()
            data = resp.json()
            return {
                "buy_price": _best_book_price(data.get("asks"), best="ask"),
                "sell_price": _best_book_price(data.get("bids"), best="bid"),
                "book_hash": data.get("hash"),
                "book_timestamp": data.get("timestamp"),
            }
        except Exception:
            if attempt == GAMMA_RETRY_ATTEMPTS - 1:
                return {"buy_price": None, "sell_price": None, "book_hash": None, "book_timestamp": None}
            await asyncio.sleep(0.3 * (attempt + 1))
    return {"buy_price": None, "sell_price": None, "book_hash": None, "book_timestamp": None}


async def llm_select_signals(
    signals: List[Dict[str, Any]],
    max_pick: int,
    *,
    mode: str = LLM_PAPER_MODE,
    url: str = LLM_PAPER_URL,
) -> List[Dict[str, Any]]:
    """Use local small LLM (OpenAI-compatible endpoint) to rank/select paper-trades.
    Falls back to heuristic ordering on any failure.
    """
    if not signals:
        return []

    # Keep prompt compact for small models.
    compact = []
    for i, s in enumerate(signals):
        compact.append({
            "i": i,
            "strategy": s.get("strategy", ""),
            "title": (s.get("event_title", "") or "")[:120],
            "side": s.get("direction", "yes"),
            "mkt_prob": round(float(s.get("market_probability", 0.5) or 0.5), 4),
            "model_prob": round(float(s.get("model_probability", 0.5) or 0.5), 4),
            "edge": round(float(s.get("edge", 0.0) or 0.0), 4),
            "conf": round(float(s.get("confidence", 0.0) or 0.0), 4),
            "size": round(float(s.get("suggested_size", 10.0) or 10.0), 2),
        })

    prompt = (
        "Você é um seletor de trades para PAPER trading (sem dinheiro real). "
        "Escolha sinais com melhor relação edge/confianca e menor risco. "
        f"Selecione no máximo {max_pick}. "
        "Responda APENAS JSON no formato: {\"picks\":[indices]}."
    )

    payload = {
        "mode": mode,
        "messages": [
            {"role": "system", "content": "Responda em JSON válido estrito, sem texto extra."},
            {"role": "user", "content": prompt + "\nSINAIS:\n" + json.dumps(compact, ensure_ascii=False)},
        ],
        "max_tokens": 220,
        "temperature": 0,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            # Small local models often add extra text; recover first JSON object.
            start = content.find("{")
            end = content.rfind("}")
            raw_json = content[start:end+1] if (start != -1 and end != -1 and end > start) else content
            try:
                obj = json.loads(raw_json)
                picks = obj.get("picks", []) if isinstance(obj, dict) else []
            except Exception:
                # Fallback: extract any integers from model text
                picks = [int(x) for x in re.findall(r"\d+", content)]
            chosen = []
            for idx in picks:
                if isinstance(idx, int) and 0 <= idx < len(signals):
                    chosen.append(signals[idx])
                if len(chosen) >= max_pick:
                    break
            if chosen:
                logger.info("LLM selector picked %d/%d signals", len(chosen), len(signals))
                return chosen
    except Exception as e:
        logger.warning("LLM selector unavailable/fallback: %s", e)

    return signals[:max_pick]


def score_signal(signal: Dict[str, Any]) -> float:
    """Composite score for ranking candidate paper orders."""
    confidence = float(signal.get("confidence", 0.0) or 0.0)
    edge = abs(float(signal.get("net_edge", signal.get("edge", 0.0)) or 0.0))
    spread = float(signal.get("spread", 0.0) or 0.0)
    liquidity = float(signal.get("liquidity", 0.0) or 0.0)
    market_probability = float(signal.get("market_probability", 0.5) or 0.5)

    liq_norm = min(liquidity / 25_000.0, 1.0)
    spread_penalty = min(spread / 0.20, 1.0)
    extremeness_penalty = abs(market_probability - 0.5) * 2.0

    return (
        (0.45 * confidence)
        + (0.35 * edge)
        + (0.20 * liq_norm)
        - (0.10 * spread_penalty)
        - (0.05 * extremeness_penalty)
    )


def entry_price_for_signal(signal: Dict[str, Any]) -> float:
    direction = str(signal.get("direction", "yes") or "yes").lower()
    market_probability = float(signal.get("market_probability", 0.5) or 0.5)
    return 1.0 - market_probability if direction == "no" else market_probability


def apply_execution_filters(signals: List[Dict[str, Any]], policy: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Apply final execution filters that are stricter than scanner discovery."""
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    min_net_edge = float(policy.get("min_net_edge", PAPER_MIN_NET_EDGE) or PAPER_MIN_NET_EDGE)
    taker_fee_estimate = float(policy.get("taker_fee_estimate", PAPER_TAKER_FEE_ESTIMATE) or 0.0)
    slippage_estimate = float(policy.get("slippage_estimate", PAPER_SLIPPAGE_ESTIMATE) or 0.0)
    shadow_strategies = {
        s.strip()
        for s in str(policy.get("shadow_strategies", PAPER_SHADOW_STRATEGIES) or "").split(",")
        if s.strip()
    }

    for signal in signals:
        s = dict(signal)
        strategy = str(s.get("strategy") or "")
        direction = str(s.get("direction", "yes") or "yes").lower()
        if direction not in {"yes", "no"}:
            rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": f"unsupported direction: {direction}"})
            continue

        spread = max(0.0, float(s.get("spread", 0.0) or 0.0))
        liquidity = float(s.get("liquidity", 0.0) or 0.0)
        volume_24hr = float(s.get("volume_24hr", 0.0) or 0.0)
        gross_edge = abs(float(s.get("edge", 0.0) or 0.0))
        cost_estimate = min(0.20, spread + taker_fee_estimate + slippage_estimate)
        net_edge = gross_edge - cost_estimate
        s["execution_cost_estimate"] = round(cost_estimate, 4)
        s["net_edge"] = round(net_edge, 4)

        if net_edge < min_net_edge:
            rejected.append({
                "market_slug": s.get("event_slug", ""),
                "strategy": strategy,
                "reason": f"net_edge {net_edge:.3f} below {min_net_edge:.3f}",
            })
            continue

        if strategy == "smart_money":
            if spread > float(policy.get("smart_money_max_spread", PAPER_SMART_MONEY_MAX_SPREAD)):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": f"spread {spread:.3f} above smart_money max"})
                continue
            if (
                liquidity < float(policy.get("smart_money_min_liquidity", PAPER_SMART_MONEY_MIN_LIQUIDITY))
                or volume_24hr < float(policy.get("smart_money_min_vol24h", PAPER_SMART_MONEY_MIN_VOL24H))
            ):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "smart_money liquidity/volume below execution threshold"})
                continue
        elif strategy == "event_countdown":
            if spread > float(policy.get("event_countdown_max_spread", PAPER_EVENT_COUNTDOWN_MAX_SPREAD)):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": f"spread {spread:.3f} above event_countdown max"})
                continue
            if (
                liquidity < float(policy.get("event_countdown_min_liquidity", PAPER_EVENT_COUNTDOWN_MIN_LIQUIDITY))
                or volume_24hr < float(policy.get("event_countdown_min_vol24h", PAPER_EVENT_COUNTDOWN_MIN_VOL24H))
            ):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "event_countdown liquidity/volume below execution threshold"})
                continue
        elif strategy == "btc_5m_momentum":
            if entry_price_for_signal(s) > float(policy.get("btc_max_entry_price", PAPER_BTC_MAX_ENTRY_PRICE_EXEC)):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "btc entry price above execution max"})
                continue
            if liquidity < float(policy.get("btc_min_liquidity", PAPER_BTC_MIN_LIQUIDITY_EXEC)):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "btc liquidity below execution threshold"})
                continue
        elif strategy == "endgame_last_minute":
            if entry_price_for_signal(s) > float(policy.get("endgame_max_entry_price", PAPER_ENDGAME_MAX_ENTRY_PRICE_EXEC)):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "endgame entry price above execution max"})
                continue
            if liquidity < float(policy.get("endgame_min_liquidity", PAPER_ENDGAME_MIN_LIQUIDITY_EXEC)):
                rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "endgame liquidity below execution threshold"})
                continue
        if strategy in shadow_strategies:
            rejected.append({"market_slug": s.get("event_slug", ""), "strategy": strategy, "reason": "strategy is configured as shadow-only"})
            continue

        accepted.append(s)

    return accepted, rejected


def select_best_orders(signals: List[Dict[str, Any]], max_pick: int) -> List[Dict[str, Any]]:
    """Select top signals with diversification by market+direction."""
    ranked = sorted(signals, key=score_signal, reverse=True)
    picks: List[Dict[str, Any]] = []
    seen_keys = set()

    for signal in ranked:
        market_key = signal.get("event_slug") or signal.get("market_id") or signal.get("market_slug") or ""
        direction = (signal.get("direction") or "yes").lower()
        dedupe_key = (market_key, direction)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        picks.append(signal)
        if len(picks) >= max_pick:
            break

    return picks


_NEGATIVE_OSINT_KEYWORDS = frozenset({
    "injury", "injured", "suspended", "cancelled", "postponed",
    "withdrawn", "scratched", "forfeit", "canceled", "retire",
})


def _extract_news_query(signal: Dict[str, Any]) -> str:
    title = (signal.get("event_title") or "").strip()
    slug = (signal.get("event_slug") or "").replace("-", " ").strip()
    raw = title or slug
    tokens = [t for t in re.findall(r"[A-Za-z0-9']+", raw) if len(t) > 2]
    return " ".join(tokens[:10])

async def _google_news_count(query: str, *, window_hours: int, max_articles: int) -> int:
    if not query:
        return 0
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    now = datetime.now(timezone.utc)
    count = 0
    try:
        async with httpx.AsyncClient(timeout=12.0, trust_env=False) as client:
            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.4 * (attempt + 1))
        root = ET.fromstring(resp.text)
        for item in root.findall("./channel/item"):
            pub = (item.findtext("pubDate") or "").strip()
            if not pub:
                continue
            try:
                dt = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            age_hours = (now - dt).total_seconds() / 3600
            if age_hours <= window_hours:
                count += 1
                if count >= max_articles:
                    break
    except Exception as e:
        logger.warning("Google News OSINT lookup failed for query='%s': %s", query, e)
    return count

async def apply_osint_google_news(signals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not OSINT_GOOGLE_NEWS_ENABLED or not signals:
        return signals

    async def enrich(signal: Dict[str, Any]) -> Dict[str, Any]:
        query = _extract_news_query(signal)
        count = await _google_news_count(
            query,
            window_hours=OSINT_GOOGLE_NEWS_WINDOW_HOURS,
            max_articles=OSINT_GOOGLE_NEWS_MAX_ARTICLES,
        )
        raw_text = query.lower()
        negative_hits = sum(1 for kw in _NEGATIVE_OSINT_KEYWORDS if kw in raw_text)
        sentiment_penalty = min(0.02, 0.005 * negative_hits)
        bonus = max(0.0, min(OSINT_GOOGLE_NEWS_BONUS_CAP, 0.005 * count - sentiment_penalty))
        enriched = dict(signal)
        enriched["osint_news_query"] = query
        enriched["osint_news_hits"] = count
        enriched["osint_negative_hits"] = negative_hits
        enriched["osint_bonus"] = round(bonus, 4)
        enriched["osint_score"] = round(min(1.0, count / max(1, OSINT_GOOGLE_NEWS_MAX_ARTICLES)), 4)
        enriched["edge"] = float(enriched.get("edge", 0.0) or 0.0) + bonus
        return enriched

    enriched_signals = await asyncio.gather(*(enrich(s) for s in signals))
    logger.info("Google News OSINT enriched %d signals", len(enriched_signals))
    return list(enriched_signals)

async def fetch_market_prices(market_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch executable side prices for market IDs.

    Open markets use CLOB executable quotes:
    - BUY is the best ask, used for new paper entries.
    - SELL is the best bid, used for risk exits.

    Closed markets use binary Gamma outcome prices only when they look resolved.
    """
    results: Dict[str, Dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=GAMMA_TIMEOUT_SECONDS, trust_env=False) as client:
        for mid in market_ids:
            try:
                for attempt in range(GAMMA_RETRY_ATTEMPTS):
                    try:
                        resp = await client.get(f"{GAMMA_API}/markets/{mid}")
                        resp.raise_for_status()
                        break
                    except Exception:
                        if attempt == GAMMA_RETRY_ATTEMPTS - 1:
                            raise
                        await asyncio.sleep(0.4 * (attempt + 1))
                data = resp.json()

                closed = bool(data.get("closed", False))
                resolved = closed  # In Polymarket, closed = resolved for our purposes
                tokens = [str(x) for x in _json_list(data.get("clobTokenIds", ""))]
                outcomes = [str(x) for x in _json_list(data.get("outcomes", ""))]

                if len(tokens) < 2:
                    raise ValueError("market has no two clobTokenIds")

                resolved_prices = resolved_side_prices_from_gamma(data) if closed else None
                if resolved_prices:
                    yes_price, no_price = resolved_prices
                    yes_buy_price = yes_sell_price = yes_price
                    no_buy_price = no_sell_price = no_price
                    price_source = "gamma_resolution"
                else:
                    yes_quote, no_quote = await asyncio.gather(
                        fetch_clob_book_quote(client, tokens[0]),
                        fetch_clob_book_quote(client, tokens[1]),
                    )
                    yes_buy_price = yes_quote.get("buy_price")
                    yes_sell_price = yes_quote.get("sell_price")
                    no_buy_price = no_quote.get("buy_price")
                    no_sell_price = no_quote.get("sell_price")
                    yes_price = yes_sell_price
                    no_price = no_sell_price
                    price_source = "clob_book"
                    if yes_sell_price is None or no_sell_price is None:
                        raise ValueError("missing CLOB SELL quote for one or more outcomes")

                results[mid] = {
                    "ok": True,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "yes_buy_price": yes_buy_price,
                    "no_buy_price": no_buy_price,
                    "yes_sell_price": yes_sell_price,
                    "no_sell_price": no_sell_price,
                    "yes_token_id": tokens[0],
                    "no_token_id": tokens[1],
                    "yes_outcome": outcomes[0] if len(outcomes) > 0 else "YES",
                    "no_outcome": outcomes[1] if len(outcomes) > 1 else "NO",
                    "yes_book_hash": yes_quote.get("book_hash") if not resolved_prices else None,
                    "no_book_hash": no_quote.get("book_hash") if not resolved_prices else None,
                    "yes_book_timestamp": yes_quote.get("book_timestamp") if not resolved_prices else None,
                    "no_book_timestamp": no_quote.get("book_timestamp") if not resolved_prices else None,
                    "price_source": price_source,
                    "closed": closed,
                    "resolved": resolved,
                    "question": data.get("question", ""),
                }
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.warning("Failed to fetch price for market %s: %s", mid, e)
                results[mid] = {
                    "ok": False,
                    "error": str(e),
                    "closed": False,
                    "resolved": False,
                    "question": "",
                }
    return results


def backup_wallet_file() -> Optional[Path]:
    """Create a pre-cycle wallet snapshot with bounded local retention."""
    if not WALLET_BACKUP_ENABLED:
        return None

    base_dir = Path(__file__).resolve().parent
    wallet_file = base_dir / "wallet.json"
    if not wallet_file.exists():
        return None

    backup_dir = base_dir / "logs" / "wallet_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"wallet-{ts}.json"
    shutil.copy2(wallet_file, target)

    backups = sorted(backup_dir.glob("wallet-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max(0, WALLET_BACKUP_RETENTION):]:
        try:
            old.unlink()
        except OSError:
            logger.warning("Failed to remove old wallet backup: %s", old)
    return target

def get_current_price(position: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    """Get the executable exit price relevant to a position's side."""
    if position["side"] == "YES":
        return market_data["yes_sell_price"]
    return market_data["no_sell_price"]


def get_entry_quote(side: str, market_data: Dict[str, Any]) -> Optional[float]:
    """Get the executable entry price for buying the selected outcome token."""
    if side == "YES":
        return market_data.get("yes_buy_price")
    return market_data.get("no_buy_price")

async def settle_positions(wallet: Optional[Wallet] = None) -> Dict[str, Any]:
    """Check all open positions and close any that hit stop-loss, take-profit,
    or are in resolved markets.

    Returns a summary dict with closed positions and stats.
    """
    if wallet is None:
        wallet = Wallet()

    open_positions = wallet.get_open_positions()
    if not open_positions:
        logger.info("No open positions to settle")
        return {"checked": 0, "closed": 0, "still_open": 0, "closings": []}

    # Collect unique market IDs — prefer numeric market_id over slug
    market_id_map: Dict[str, str] = {}  # position_id -> lookup_key
    for pos in open_positions:
        mid = pos.get("market_id", "")
        slug = pos.get("market_slug", "")
        # Prefer numeric market_id for Gamma API /markets/{id} endpoint
        if mid and mid.isdigit():
            market_id_map[pos["id"]] = mid
        elif slug and slug.isdigit():
            market_id_map[pos["id"]] = slug
        elif mid:
            market_id_map[pos["id"]] = mid
        else:
            market_id_map[pos["id"]] = slug

    unique_ids = list(set(v for v in market_id_map.values() if v))

    logger.info("Fetching prices for %d markets (%d open positions)", len(unique_ids), len(open_positions))

    market_prices = await fetch_market_prices(unique_ids)

    closings: List[Dict[str, Any]] = []
    checked = 0

    for pos in open_positions:
        pid = pos["id"]
        lookup_key = market_id_map.get(pid, "")

        mdata = market_prices.get(lookup_key)
        if not mdata:
            logger.warning("No price data for position %s (market=%s), skipping", pid[:8], lookup_key)
            continue
        if not mdata.get("ok", False):
            logger.warning("Unreliable price data for position %s (market=%s), skipping risk check", pid[:8], lookup_key)
            continue

        checked += 1
        current_price = get_current_price(pos, mdata)
        pos["current_exit_price_source"] = mdata.get("price_source")
        pos["exit_execution_model"] = "conservative_buy_ask_sell_bid"

        # Check 1: Market resolved/closed
        if mdata.get("resolved"):
            logger.info("Market resolved for position %s — closing at %.2f", pid[:8], current_price)
            try:
                pos["close_price_source"] = mdata.get("price_source")
                closed = wallet.close_position(pid, current_price, reason="market_resolved")
                closings.append({"position_id": pid, "reason": "market_resolved", "pnl": closed["pnl"]})
            except Exception as e:
                logger.error("Failed to close resolved position %s: %s", pid[:8], e)
            continue

        # Check 2: Stop-loss / take-profit
        try:
            pos["close_price_source"] = mdata.get("price_source")
            result = wallet.check_risk_exit(pid, current_price)
            if result:
                logger.info("Risk exit triggered for %s: %s (P&L=%.2f)", pid[:8], result["close_reason"], result["pnl"])
                closings.append({"position_id": pid, "reason": result["close_reason"], "pnl": result["pnl"]})
        except Exception as e:
            logger.error("Risk check failed for position %s: %s", pid[:8], e)

    still_open = len(wallet.get_open_positions())
    status = wallet.get_status()

    logger.info("Settlement complete: checked=%d, closed=%d, still_open=%d", checked, len(closings), still_open)

    return {

        "checked": checked,
        "closed": len(closings),
        "still_open": still_open,
        "closings": closings,
        "bankroll": status["bankroll"],
        "total_exposure": status["total_exposure"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

async def _filter_and_rank_signals(
    all_signals: List[Dict[str, Any]],
    selected: set,
    btc_only: bool,
    effective_min_edge: float,
    strategy_multipliers: Dict[str, float],
    execution_policy: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Filter signals by strategy/edge, enrich with OSINT, apply policy filters, rank."""
    signals = [s for s in all_signals if s.get("strategy") in selected]
    actionable = [s for s in signals if abs(s.get("edge", 0)) >= effective_min_edge]
    # OSINT enrichment skipped for pure BTC 5m scalps (news irrelevant on 5-min horizon)
    if not btc_only:
        actionable = await apply_osint_google_news(actionable)
    actionable, policy_rejected = apply_execution_filters(actionable, execution_policy)
    for s in actionable:
        mult = float(strategy_multipliers.get(str(s.get("strategy") or ""), 1.0) or 1.0)
        s["_learn_score"] = float(s.get("net_edge", s.get("edge", 0)) or 0) * mult
    actionable.sort(key=lambda x: float(x.get("_learn_score", x.get("net_edge", x.get("edge", 0))) or 0), reverse=True)
    return actionable, policy_rejected


def _streak_size_multiplier(history: List[Dict[str, Any]], settings: Dict[str, Any], *, max_lookback: int = 6) -> float:
    """Scale position size by the trailing win/loss streak.

    Mirrors the trailing-streak walk in ops_runtime.circuit_breaker (losses_seq):
    walk backwards from the most recent trusted close while the PnL sign holds.
    Consecutive losses shrink size (risk-off after a bad run); consecutive wins
    grow it moderately (both capped via settings so neither runs away).
    """
    hist = [h for h in history if h.get("trusted_for_pnl", True)]
    last = hist[-max_lookback:]
    pnls = [float(x.get("pnl", 0.0) or 0.0) for x in last]
    if not pnls:
        return 1.0

    streak_len = 0
    sign = 0
    for p in reversed(pnls):
        current_sign = 1 if p > 0 else (-1 if p < 0 else 0)
        if current_sign == 0:
            break
        if sign == 0:
            sign = current_sign
        if current_sign != sign:
            break
        streak_len += 1

    if sign < 0:
        min_mult = float(settings.get("streak_size_min_mult", 0.5) or 0.5)
        decay = float(settings.get("streak_loss_decay", 0.12) or 0.12)
        return max(min_mult, 1.0 - decay * streak_len)
    if sign > 0:
        max_mult = float(settings.get("streak_size_max_mult", 1.3) or 1.3)
        boost = float(settings.get("streak_win_boost", 0.07) or 0.07)
        return min(max_mult, 1.0 + boost * streak_len)
    return 1.0


async def _execute_trades(
    top_signals: List[Dict[str, Any]],
    wallet: "Wallet",
    execution_policy: Dict[str, Any],
    min_trade: float,
    max_trade: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Open paper positions for each top signal; return (executed, skipped) lists."""
    executed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for sig in top_signals:
        try:
            raw_direction = str(sig.get("direction", "yes") or "yes").lower()
            if raw_direction not in {"yes", "no"}:
                skipped.append({"market_slug": sig.get("event_slug", ""), "reason": f"unsupported signal direction for paper execution: {raw_direction}", "strategy": sig.get("strategy", "")})
                continue

            direction = raw_direction.upper()
            market_lookup = str(sig.get("market_id", "") or "")
            if not market_lookup:
                skipped.append({"market_slug": sig.get("event_slug", ""), "reason": "missing market_id for CLOB execution quote", "strategy": sig.get("strategy", "")})
                continue

            quote_data = (await fetch_market_prices([market_lookup])).get(market_lookup)
            if not quote_data or not quote_data.get("ok"):
                skipped.append({"market_slug": sig.get("event_slug", ""), "reason": "missing executable CLOB quote", "strategy": sig.get("strategy", "")})
                continue

            entry_price = get_entry_quote(direction, quote_data)
            if entry_price is None:
                skipped.append({"market_slug": sig.get("event_slug", ""), "reason": f"missing {direction} BUY quote", "strategy": sig.get("strategy", "")})
                continue

            strat = str(sig.get("strategy", "") or "")
            if strat == "btc_5m_momentum" and entry_price > float(execution_policy.get("btc_max_entry_price", PAPER_BTC_MAX_ENTRY_PRICE_EXEC)):
                skipped.append({"market_slug": sig.get("event_slug", ""), "strategy": strat, "reason": "btc executable entry price above max"})
                continue
            if strat == "endgame_last_minute" and entry_price > float(execution_policy.get("endgame_max_entry_price", PAPER_ENDGAME_MAX_ENTRY_PRICE_EXEC)):
                skipped.append({"market_slug": sig.get("event_slug", ""), "strategy": strat, "reason": "endgame executable entry price above max"})
                continue

            streak_mult = _streak_size_multiplier(wallet.state.get("history", []), wallet.state.get("settings", {}))
            size = max(min_trade, min(sig.get("suggested_size", min_trade) * streak_mult, max_trade))
            market_slug = sig.get("event_slug", sig.get("market_id", ""))

            pos = wallet.open_position(
                market_slug=market_slug,
                side=direction,
                price=entry_price,
                size=size,
                edge=sig.get("edge"),
                extra={
                    "market_id": sig.get("market_id", ""),
                    "event_title": sig.get("event_title", ""),
                    "strategy": sig.get("strategy", ""),
                    "model_probability": sig.get("model_probability"),
                    "market_probability": sig.get("market_probability"),
                    "confidence": sig.get("confidence"),
                    "kelly_fraction": sig.get("kelly_fraction"),
                    "streak_multiplier": round(streak_mult, 4),
                    "price_source": quote_data.get("price_source"),
                    "execution_model": "conservative_buy_ask_sell_bid",
                    "execution_token_id": quote_data.get("yes_token_id") if direction == "YES" else quote_data.get("no_token_id"),
                    "execution_outcome": quote_data.get("yes_outcome") if direction == "YES" else quote_data.get("no_outcome"),
                    "trusted_for_pnl": True,
                },
            )
            executed.append({
                "market_slug": market_slug,
                "side": direction,
                "entry_price": entry_price,
                "price_source": quote_data.get("price_source"),
                "size": size,
                "cost": pos["cost"],
                "edge": sig.get("edge", 0),
                "strategy": sig.get("strategy", ""),
                "event_title": sig.get("event_title", ""),
            })
            logger.info("EXECUTED: %s %s %s @%.2f size=$%.2f edge=%.1f%%",
                        sig.get("strategy", ""), direction, market_slug[:30], entry_price, size, sig.get("edge", 0) * 100)
        except ValueError as e:
            skipped.append({"market_slug": sig.get("event_slug", ""), "reason": str(e)})
        except Exception as e:
            logger.error("Failed to execute signal: %s", e)
            skipped.append({"market_slug": sig.get("event_slug", ""), "reason": f"error: {e}"})

    return executed, skipped


def _record_learning(
    cycle_id: str,
    all_signals: List[Dict[str, Any]],
    selected: set,
    effective_min_edge: float,
    actionable: List[Dict[str, Any]],
    policy_rejected: List[Dict[str, Any]],
    top_signals: List[Dict[str, Any]],
    executed: List[Dict[str, Any]],
    skipped: List[Dict[str, Any]],
    outcome_summary: Dict[str, Any],
    entries_paused: bool = False,
) -> Dict[str, Any]:
    """Record signal decisions in the learning store and return the store summary."""
    try:
        decision_summary = record_signal_decisions(
            cycle_id=cycle_id,
            signals=all_signals,
            selected_strategies=selected,
            effective_min_edge=effective_min_edge,
            accepted_signals=actionable,
            policy_rejected=policy_rejected,
            top_signals=top_signals,
            executed=executed,
            skipped=skipped,
            entries_paused=entries_paused,
        )
        store_summary = summarize_learning_events()
        store_summary.update({"last_cycle_decisions": decision_summary, "last_cycle_outcomes": outcome_summary})
        return store_summary
    except Exception as e:
        logger.warning("Learning signal decision recording failed: %s", e)
        return {"error": str(e)}


async def scan_and_trade(wallet: Optional[Wallet] = None) -> Dict[str, Any]:
    """Full cycle: scan strategies → execute trades → settle positions.
    Returns a comprehensive report dict.
    """
    if wallet is None:
        wallet = Wallet()
    cycle_id = new_cycle_id()

    base_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(base_dir))
    from scanner import scan_all, scan_btc_5m_only

    # Phase 1: settle existing positions
    settlement_report = await settle_positions(wallet)

    selected = _selected_strategies(PAPER_STRATEGY_MODE)
    btc_only = selected == {'btc_5m_momentum'}

    # Phase 2: scan for new signals
    scan_result = await (scan_btc_5m_only() if btc_only else scan_all())
    all_signals = list(scan_result.get("signals", []) or [])

    try:
        outcome_summary = observe_signal_outcomes_from_signals(all_signals)
    except Exception as e:
        logger.warning("Learning signal outcome update failed: %s", e)
        outcome_summary = {"enabled": True, "error": str(e), "outcome_events": 0, "pending": 0}

    # Phase 3: apply learning policy
    settings = wallet.state.get("settings", {})
    min_edge = settings.get("min_edge", 0.05)
    wallet.state["last_strategy_mode"] = PAPER_STRATEGY_MODE
    ls = ensure_learning_state(wallet.state)
    policy = maybe_refresh_policy(ls, settings)
    learning_enabled = bool(ls.get("enabled", True))
    learning_shadow = bool(ls.get("shadow_mode", False))
    effective_min_edge = float(policy.get("effective_min_edge", min_edge) or min_edge)
    if (not learning_enabled) or learning_shadow:
        effective_min_edge = float(min_edge)
    strategy_multipliers = dict(policy.get("strategy_multipliers") or {})

    if PAPER_DISABLE_NEW_ENTRIES:
        skipped_paused = [{"market_slug": "*", "reason": "entries paused by circuit breaker"}]
        store_summary = _record_learning(cycle_id, all_signals, selected, effective_min_edge, [], [], [], [], skipped_paused, outcome_summary, entries_paused=True)
        wallet.save()
        return {
            "settlement": settlement_report,
            "scan": {"total_markets": scan_result.get("total_markets", 0), "total_signals": scan_result.get("total_signals", 0), "actionable_signals": 0, "by_strategy": scan_result.get("by_strategy", {}), "strategy_mode": PAPER_STRATEGY_MODE, "min_edge_base": min_edge, "min_edge_effective": min_edge},
            "execution": {"attempted": 0, "executed": 0, "skipped": 1, "trades": [], "skipped_reasons": skipped_paused},
            "wallet": wallet.get_status(),
            "learning": learning_snapshot(ls),
            "learning_store": store_summary,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    execution_policy = execution_policy_from_settings(settings)
    llm_enabled = bool(settings.get("llm_enabled", LLM_PAPER_ENABLED))
    llm_mode = str(settings.get("llm_mode", LLM_PAPER_MODE) or LLM_PAPER_MODE)
    llm_url = str(settings.get("llm_url", LLM_PAPER_URL) or LLM_PAPER_URL)
    min_trade = settings.get("min_trade", 10)
    max_trade = settings.get("max_trade", 50)
    max_per_scan = settings.get("max_per_scan", 10)

    # Phase 4: filter, rank, optional LLM re-rank
    actionable, policy_rejected = await _filter_and_rank_signals(all_signals, selected, btc_only, effective_min_edge, strategy_multipliers, execution_policy)
    top_signals = select_best_orders(actionable, max_per_scan)
    if llm_enabled and top_signals:
        top_signals = await llm_select_signals(top_signals, max_per_scan, mode=llm_mode, url=llm_url)

    # Phase 5: open paper positions
    executed, skipped = await _execute_trades(top_signals, wallet, execution_policy, min_trade, max_trade)

    # Phase 6: learning store + save
    store_summary = _record_learning(cycle_id, all_signals, selected, effective_min_edge, actionable, policy_rejected, top_signals, executed, skipped, outcome_summary)
    wallet.save()

    return {
        "settlement": settlement_report,
        "scan": {
            "total_markets": scan_result.get("total_markets", 0),
            "total_signals": scan_result.get("total_signals", 0),
            "actionable_signals": len(actionable),
            "by_strategy": scan_result.get("by_strategy", {}),
            "strategy_mode": PAPER_STRATEGY_MODE,
            "min_edge_base": min_edge,
            "min_edge_effective": effective_min_edge,
            "min_net_edge": execution_policy.get("min_net_edge"),
            "policy_rejected_signals": len(policy_rejected),
        },
        "execution": {
            "attempted": len(top_signals),
            "executed": len(executed),
            "skipped": len(skipped),
            "policy_rejected": len(policy_rejected),
            "trades": executed,
            "skipped_reasons": (skipped + policy_rejected)[:5],
            "execution_policy": execution_policy,
            "llm_enabled": llm_enabled,
            "llm_mode": llm_mode,
        },
        "wallet": wallet.get_status(),
        "learning": learning_snapshot(ls),
        "learning_store": store_summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

def format_report(report: Dict[str, Any]) -> str:
    """Format a scan_and_trade report into a human-readable string."""
    lines = []
    lines.append("📊 **Polymarket Trading Report**")
    lines.append("")

    # Settlement
    s = report.get("settlement", {})
    lines.append(f"🔄 **Settlement:** {s.get('checked', 0)} checked, {s.get('closed', 0)} closed, {s.get('still_open', 0)} still open")
    for c in s.get("closings", []):
        reason = c.get("reason", "?")
        pnl = c.get("pnl", 0)
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(f"  {emoji} {reason}: P&L ${pnl:+.2f}")

    # Scan
    sc = report.get("scan", {})
    lines.append("")
    lines.append(f"🔍 **Scan:** {sc.get('total_markets', 0)} markets, {sc.get('total_signals', 0)} signals ({sc.get('actionable_signals', 0)} actionable) | mode={sc.get('strategy_mode', 'all')}")
    if sc.get('min_edge_effective') is not None:
        lines.append(f"  🧠 learning min_edge: base={float(sc.get('min_edge_base', 0)):.3f} → efetivo={float(sc.get('min_edge_effective', 0)):.3f}")
    if sc.get('min_net_edge') is not None:
        lines.append(f"  🧮 execution min_net_edge={float(sc.get('min_net_edge', 0)):.3f} | policy_rejected={int(sc.get('policy_rejected_signals', 0) or 0)}")
    by_strat = sc.get("by_strategy", {})
    if by_strat:
        lines.append("  " + " | ".join(f"{k}: {v}" for k, v in by_strat.items()))

    # Execution
    ex = report.get("execution", {})
    lines.append("")
    lines.append(f"💰 **Trades:** {ex.get('executed', 0)} opened, {ex.get('skipped', 0)} skipped, {ex.get('policy_rejected', 0)} policy-rejected")
    for t in ex.get("trades", [])[:5]:

        lines.append(f"  ✅ {t.get('strategy', '?')} {t.get('side', '?')} {t.get('event_title', '?')[:35]} @ {t.get('entry_price', 0):.2f} ${t.get('size', 0):.0f} edge={t.get('edge', 0):.1%}")

    # Wallet
    w = report.get("wallet", {})
    lines.append("")
    lines.append(f"💳 **Wallet:** ${w.get('bankroll', 0):,.0f} | Exposure: ${w.get('total_exposure', 0):,.0f} | Open: {w.get('open_positions', 0)} | History: {w.get('history_count', 0)}")

    lrn = report.get("learning", {})
    if lrn:
        lines.append(f"🧠 **Learning:** enabled={lrn.get('enabled')} shadow={lrn.get('shadow_mode')} eff_min_edge={float(lrn.get('effective_min_edge', 0)):.3f} conf={lrn.get('confidence', '-')}")
    store = report.get("learning_store", {})
    if store:
        cycle_decisions = store.get("last_cycle_decisions", {}) if isinstance(store.get("last_cycle_decisions"), dict) else {}
        cycle_outcomes = store.get("last_cycle_outcomes", {}) if isinstance(store.get("last_cycle_outcomes"), dict) else {}
        lines.append(
            "🧾 **Signal store:** "
            f"decisions={int(cycle_decisions.get('decision_events', 0) or 0)} "
            f"outcomes={int(cycle_outcomes.get('outcome_events', 0) or 0)} "
            f"pending={int(cycle_decisions.get('pending', cycle_outcomes.get('pending', 0)) or 0)}"
        )

    return "\n".join(lines)

if __name__ == "__main__":
    async def _main():
        mode = sys.argv[1] if len(sys.argv) > 1 else "full"
        backup_wallet_file()
        wallet = Wallet()

        if mode == "settle":
            settle_result = await settle_positions(wallet)
            report = {
                "settlement": settle_result,
                "scan": {"total_markets": 0, "total_signals": 0, "actionable_signals": 0, "by_strategy": {}},
                "execution": {"attempted": 0, "executed": 0, "skipped": 0, "trades": [], "skipped_reasons": []},
                "wallet": wallet.get_status(),
            }
        elif mode == "full":
            report = await scan_and_trade(wallet)
        else:
            print("Usage: python settlement.py [settle|full]")
            return

        base_dir = Path(__file__).resolve().parent
        logs_dir = base_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "last_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        print(format_report(report))

    asyncio.run(_main())
