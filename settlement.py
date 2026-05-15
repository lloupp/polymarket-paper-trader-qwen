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
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from wallet import Wallet

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("polymarket-settlement")

GAMMA_API = "https://gamma-api.polymarket.com"

LLM_PAPER_ENABLED = os.getenv("PAPER_LLM_ENABLED", "0") == "1"
LLM_PAPER_URL = os.getenv("PAPER_LLM_URL", "http://127.0.0.1:8080/v1/chat/completions")
LLM_PAPER_MODE = os.getenv("PAPER_LLM_MODE", "balanced")


async def llm_select_signals(signals: List[Dict[str, Any]], max_pick: int) -> List[Dict[str, Any]]:
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
        "mode": LLM_PAPER_MODE,
        "messages": [
            {"role": "system", "content": "Responda em JSON válido estrito, sem texto extra."},
            {"role": "user", "content": prompt + "\nSINAIS:\n" + json.dumps(compact, ensure_ascii=False)},
        ],
        "max_tokens": 220,
        "temperature": 0,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(LLM_PAPER_URL, json=payload)
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
    """Composite score to find better paper orders.

    Prioritizes edge and confidence, then favors liquid/tighter markets and
    slightly penalizes expensive entries near 1.0 (worse payoff asymmetry).
    """
    edge = abs(float(signal.get("edge", 0.0) or 0.0))
    conf = float(signal.get("confidence", 0.0) or 0.0)
    spread = float(signal.get("spread", 0.02) or 0.02)
    liquidity = float(signal.get("liquidity", 0.0) or 0.0)
    market_prob = float(signal.get("market_probability", 0.5) or 0.5)

    edge_conf = edge * (0.5 + conf)
    spread_bonus = max(0.0, 0.03 - spread)
    liq_bonus = min(liquidity / 100000.0, 0.2)
    payoff_penalty = max(0.0, market_prob - 0.85) * 0.3

    return edge_conf + spread_bonus + liq_bonus - payoff_penalty


def select_best_orders(signals: List[Dict[str, Any]], max_pick: int) -> List[Dict[str, Any]]:
    """Select best candidate orders with diversification by event/market."""
    ranked = sorted(signals, key=score_signal, reverse=True)
    selected: List[Dict[str, Any]] = []
    seen_markets = set()

    for s in ranked:
        key = (s.get("event_slug") or s.get("market_id") or "", s.get("direction", "yes").upper())
        if key in seen_markets:
            continue
        selected.append(s)
        seen_markets.add(key)
        if len(selected) >= max_pick:
            break
    return selected

async def fetch_market_prices(market_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch current prices for a list of market IDs from Gamma API.
    Returns {market_id: {"yes_price": float, "no_price": float, "closed": bool, "resolved": bool}}
    """
    results: Dict[str, Dict[str, Any]] = {}
    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
        for mid in market_ids:
            try:
                resp = await client.get(f"{GAMMA_API}/markets/{mid}")
                resp.raise_for_status()
                data = resp.json()

                yes_price, no_price = 0.5, 0.5
                prices = data.get("outcomePrices", "")
                if prices:
                    try:
                        p = json.loads(prices) if isinstance(prices, str) else prices
                        if isinstance(p, list) and len(p) >= 2:
                            yes_price, no_price = float(p[0]), float(p[1])
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass

                closed = bool(data.get("closed", False))
                resolved = closed  # In Polymarket, closed = resolved for our purposes

                results[mid] = {
                    "yes_price": yes_price,
                    "no_price": no_price,

                    "closed": closed,
                    "resolved": resolved,
                    "question": data.get("question", ""),
                }
                await asyncio.sleep(0.2)  # rate limit
            except Exception as e:
                logger.warning("Failed to fetch price for market %s: %s", mid, e)
                results[mid] = {"yes_price": 0.5, "no_price": 0.5, "closed": False, "resolved": False, "question": ""}
    return results

def get_current_price(position: Dict[str, Any], market_data: Dict[str, Any]) -> float:
    """Get the current price relevant to a position's side."""
    if position["side"] == "YES":
        return market_data["yes_price"]
    else:
        return market_data["no_price"]

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

        checked += 1
        current_price = get_current_price(pos, mdata)

        # Check 1: Market resolved/closed
        if mdata.get("resolved"):
            logger.info("Market resolved for position %s — closing at %.2f", pid[:8], current_price)
            try:
                closed = wallet.close_position(pid, current_price, reason="market_resolved")
                closings.append({"position_id": pid, "reason": "market_resolved", "pnl": closed["pnl"]})
            except Exception as e:
                logger.error("Failed to close resolved position %s: %s", pid[:8], e)
            continue

        # Check 2: Stop-loss / take-profit
        try:
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

async def scan_and_trade(wallet: Optional[Wallet] = None) -> Dict[str, Any]:
    """Full cycle: scan strategies → execute trades → settle positions.
    Returns a comprehensive report dict.
    """
    if wallet is None:
        wallet = Wallet()

    # Import scanner
    base_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(base_dir))
    from scanner import scan_all

    # Phase 1: Settle existing positions first
    settlement_report = await settle_positions(wallet)

    # Phase 2: Scan for new signals
    scan_result = await scan_all()

    # Phase 3: Execute top signals
    signals = scan_result.get("signals", [])
    max_per_scan = wallet.state.get("settings", {}).get("max_per_scan", 10)
    min_edge = wallet.state.get("settings", {}).get("min_edge", 0.05)
    min_trade = wallet.state.get("settings", {}).get("min_trade", 10)
    max_trade = wallet.state.get("settings", {}).get("max_trade", 50)

    # Filter actionable signals
    actionable = [s for s in signals if abs(s.get("edge", 0)) >= min_edge]
    # Select best orders with a richer scoring (edge/confidence/liquidity/spread).
    top_signals = select_best_orders(actionable, max_per_scan)
    # Optional local LLM pass to re-rank selected candidates.
    if LLM_PAPER_ENABLED and top_signals:
        top_signals = await llm_select_signals(top_signals, max_per_scan)

    executed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for sig in top_signals:
        try:
            direction = sig.get("direction", "yes").upper()
            if direction not in ("YES", "NO"):
                direction = "YES"

            entry_price = sig.get("market_probability", 0.5)
            if direction == "NO":
                entry_price = 1.0 - entry_price

            suggested_size = sig.get("suggested_size", min_trade)
            size = max(min_trade, min(suggested_size, max_trade))

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
                    "confidence": sig.get("confidence"),
                    "kelly_fraction": sig.get("kelly_fraction"),
                },
            )
            executed.append({
                "market_slug": market_slug,
                "side": direction,
                "entry_price": entry_price,
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

    status = wallet.get_status()

    return {
        "settlement": settlement_report,
        "scan": {

            "total_markets": scan_result.get("total_markets", 0),
            "total_signals": scan_result.get("total_signals", 0),
            "actionable_signals": len(actionable),
            "by_strategy": scan_result.get("by_strategy", {}),
        },
        "execution": {
            "attempted": len(top_signals),
            "executed": len(executed),
            "skipped": len(skipped),
            "trades": executed,
            "skipped_reasons": skipped[:5],  # top 5 skip reasons
        },
        "wallet": status,
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
    lines.append(f"🔍 **Scan:** {sc.get('total_markets', 0)} markets, {sc.get('total_signals', 0)} signals ({sc.get('actionable_signals', 0)} actionable)")
    by_strat = sc.get("by_strategy", {})
    if by_strat:
        lines.append("  " + " | ".join(f"{k}: {v}" for k, v in by_strat.items()))

    # Execution
    ex = report.get("execution", {})
    lines.append("")
    lines.append(f"💰 **Trades:** {ex.get('executed', 0)} opened, {ex.get('skipped', 0)} skipped")
    for t in ex.get("trades", [])[:5]:

        lines.append(f"  ✅ {t.get('strategy', '?')} {t.get('side', '?')} {t.get('event_title', '?')[:35]} @ {t.get('entry_price', 0):.2f} ${t.get('size', 0):.0f} edge={t.get('edge', 0):.1%}")

    # Wallet
    w = report.get("wallet", {})
    lines.append("")
    lines.append(f"💳 **Wallet:** ${w.get('bankroll', 0):,.0f} | Exposure: ${w.get('total_exposure', 0):,.0f} | Open: {w.get('open_positions', 0)} | History: {w.get('history_count', 0)}")

    return "\n".join(lines)

if __name__ == "__main__":
    async def _main():
        mode = sys.argv[1] if len(sys.argv) > 1 else "full"
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
            print(f"Usage: python settlement.py [settle|full]")
            return

        print(format_report(report))

    asyncio.run(_main())

