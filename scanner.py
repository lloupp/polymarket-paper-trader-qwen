"""Standalone async scanner — fetches Polymarket Gamma API and runs 5 strategies.

Strategies:
  1. detect_arbitrage      — mutually-exclusive markets summing > 1 + min_profit
  2. detect_mean_reversion — fade big 7d moves, skip same-day, 0.08-0.92 range
  3. detect_value_betting  — high vol near 50%, low liq+high vol, contrarian
  4. detect_volume_spikes  — vol/liq>3x or vol/cat_avg>3x
  5. detect_smart_money    — tight spread + high vol ratio

Usage:
  python scanner.py                # run scan_all and print summary
  from scanner import scan_all     # import as library
"""
import asyncio
import json
import logging
import os
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("polymarket-scanner")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GAMMA_API = os.getenv("POLYMARKET_GAMMA_API", "https://gamma-api.polymarket.com")

# Arbitrage
ARB_MIN_PROFIT_PCT = 0.01   # 1% minimum arb profit
ARB_MAX_PROFIT_PCT = 0.15   # 15% max — beyond this, almost certainly not mutually exclusive
ARB_MIN_ACTIVE_MARKETS = 2

# General
ADVANCED_MIN_EDGE = 0.05    # 5% minimum edge to be actionable
ADVANCED_KELLY_FRACTION = 0.25  # quarter-Kelly
INITIAL_BANKROLL = 10000.0
ADVANCED_MAX_TRADE_SIZE = 50.0

# Mean Reversion
MEAN_REVERSION_THRESHOLD = 0.10   # 10% weekly move (primary)
MEAN_REVERSION_1D_THRESHOLD = 0.10  # 10% daily move (fallback)
MEAN_REVERSION_USE_1D = True

# Value Betting
VALUE_MIN_VOLUME = 50000.0         # $50K 24h volume
VALUE_DIVERGENCE_THRESHOLD = 0.15  # 15% from 50%

# Volume Spike
VOLUME_SPIKE_MULTIPLIER = 3.0   # 3x vol/liq or vol/cat_avg
VOLUME_SPIKE_MIN_VOL = 50000.0  # $50K 24h volume minimum

# Smart Money
SMART_MONEY_MAX_SPREAD = 0.05    # 5 cent spread (task spec); runtime default 0.01
SMART_MONEY_VOLUME_RATIO = 2.0   # 2x category average
SMART_MONEY_MIN_LIQUIDITY = 30000.0

# ---------------------------------------------------------------------------
# Keyword heuristics for arbitrage non-exclusion filter
# ---------------------------------------------------------------------------
CUMULATIVE_KW = [
    " by ", " before ", " by end of ", " by the end of ",
    "first ", "second ", "third ",
    " 2024", " 2025", " 2026", " 2027",
]

THRESHOLD_KW = [
    "above", "below", "over", "under", "more than", "less than",
    "at least", "at most", "exceeds", "surpass", "minimum", "maximum",
    "higher than", "lower than", "greater than", "fewer than",
    "under ", "above $", "below $", "over $",
    "1-100", "101-", "1k-", "2.5k-", "5k-", "10k-", "25k-", "100k-",
    ">100k", ">10k", ">1k",
]

NON_EXCLUSIVE_TITLES = [
    "top 4", "top 3", "top 5", "top 6", "top 8", "top 10",
    "make playoffs", "qualify", "advance",
    "win the 2026 fifa world cup", "win eurovision", "world cup winner",
    "miss universe", "miss world", "miss international",
    "next country to", "next country",
]

POLITICAL_PARTY_PAIRS = [
    ("republican", "democrat"), ("republican", "democratic"),
    ("gop", "democrat"), ("gop", "democratic"),
    ("conservative", "labour"), ("tories", "labour"),
]

OVERROUND_CATEGORIES = [
    "election winner", "governor election", "senate election",
    "presidential election", "primary winner",
]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class GammaMarket:
    """Parsed market from Gamma API."""
    market_id: str
    event_id: str = ""
    event_slug: str = ""
    event_title: str = ""
    question: str = ""
    category: str = ""
    yes_price: float = 0.5
    no_price: float = 0.5
    volume: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    volume_24hr: float = 0.0
    volume_7d: float = 0.0
    price_change_7d: float = 0.0
    price_change_1d: float = 0.0
    end_date: Optional[datetime] = None
    active: bool = True
    closed: bool = False
    group_item_title: str = ""

@dataclass
class Signal:
    """A signal from one of the 5 strategies."""
    strategy: str
    market_id: str = ""
    event_slug: str = ""
    event_title: str = ""
    direction: str = "yes"
    model_probability: float = 0.5
    market_probability: float = 0.5
    edge: float = 0.0
    confidence: float = 0.5
    kelly_fraction: float = 0.0
    suggested_size: float = 0.0
    reasoning: str = ""
    sources: List[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Arbitrage-specific
    arb_profit_pct: float = 0.0
    arb_markets: List[str] = field(default_factory=list)

    @property
    def passes_threshold(self) -> bool:
        return abs(self.edge) >= ADVANCED_MIN_EDGE

    def to_dict(self) -> Dict[str, Any]:
        """Serialize signal to a plain dict (JSON-safe)."""
        return {
            "strategy": self.strategy,
            "market_id": self.market_id,
            "event_slug": self.event_slug,
            "event_title": self.event_title,
            "direction": self.direction,
            "model_probability": round(self.model_probability, 4),
            "market_probability": round(self.market_probability, 4),
            "edge": round(self.edge, 4),
            "confidence": round(self.confidence, 4),
            "kelly_fraction": round(self.kelly_fraction, 6),
            "suggested_size": round(self.suggested_size, 2),
            "reasoning": self.reasoning,
            "sources": self.sources,
            "timestamp": self.timestamp.isoformat(),
            "arb_profit_pct": round(self.arb_profit_pct, 2),
            "arb_markets": self.arb_markets,
        }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_non_exclusive(titles: List[str], event_title: str) -> bool:
    """Return True if these markets are clearly NOT mutually exclusive."""
    all_text = " ".join(titles).lower()
    event_lower = event_title.lower()
    if any(kw in all_text for kw in THRESHOLD_KW):
        return True
    if any(kw in all_text for kw in CUMULATIVE_KW):
        return True
    if any(kw in event_lower for kw in NON_EXCLUSIVE_TITLES):
        return True
    if any(p1 in all_text and p2 in all_text for p1, p2 in POLITICAL_PARTY_PAIRS):
        return True
    if any(kw in event_lower for kw in OVERROUND_CATEGORIES):
        return True
    return False

def kelly_size(
    win_prob: float,
    entry_price: float,
    *,
    max_fraction: float = 0.05,
    conservative: float = 1.0,
) -> Tuple[float, float]:
    """Calculate fractional-Kelly position size.

    Kelly formula: f* = (bp - q) / b
      where b = odds = (1 - entry_price) / entry_price
            p = win_prob
            q = 1 - p

    Returns (kelly_frac, dollar_size).
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0, 0.0

    b = (1.0 - entry_price) / entry_price  # decimal odds
    p = win_prob
    q = 1.0 - p

    kelly_raw = (b * p - q) / b  # f* = (bp - q) / b
    kelly_raw = max(0.0, kelly_raw)

    # Apply fractional Kelly + conservative multiplier
    kelly_frac = kelly_raw * ADVANCED_KELLY_FRACTION * conservative
    kelly_frac = min(kelly_frac, max_fraction)

    size = min(kelly_frac * INITIAL_BANKROLL, ADVANCED_MAX_TRADE_SIZE)
    return kelly_frac, size

def category_avg_volumes(markets: List[GammaMarket], attr: str = "volume_24hr") -> Dict[str, float]:
    """Return {category: average attr value} for a list of GammaMarket objects."""
    buckets: Dict[str, list] = defaultdict(list)
    for m in markets:
        if m.category:
            buckets[m.category].append(getattr(m, attr, 0))
    return {cat: sum(v) / len(v) for cat, v in buckets.items() if v}

def _slug_or_question(m: GammaMarket, limit: int = 50) -> str:
    return (m.event_slug or m.question[:limit])[:limit]

def _title_or_question(m: GammaMarket, limit: int = 80) -> str:
    return (m.event_title or m.question[:limit])[:limit]

def _direction_from_price_change(m: GammaMarket) -> str:
    """Determine direction from 1d price change; fallback to cheap side."""
    if m.price_change_1d > 0.02:
        return "yes"
    if m.price_change_1d < -0.02:
        return "no"
    return "yes" if m.yes_price < 0.5 else "no"

# ---------------------------------------------------------------------------
# Market fetcher
# ---------------------------------------------------------------------------

def _parse_market(m: dict) -> Optional[GammaMarket]:
    """Parse a single Gamma API market dict into GammaMarket."""
    mid = str(m.get("id", ""))
    if not mid:
        return None

    # Outcome prices (double-encoded JSON string)
    yes_price, no_price = 0.5, 0.5
    prices = m.get("outcomePrices", "")
    if prices:
        try:
            p = json.loads(prices) if isinstance(prices, str) else prices
            if isinstance(p, list) and len(p) >= 2:
                yes_price, no_price = float(p[0]), float(p[1])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Spread
    best_bid = float(m.get("bestBid", 0) or 0)
    best_ask = float(m.get("bestAsk", 0) or 0)
    spread = (best_ask - best_bid) if best_bid > 0 and best_ask > 0 else abs(1.0 - yes_price - no_price)

    # End date
    end_date = None
    end_str = m.get("endDate") or m.get("end_date_iso", "")
    if end_str:
        try:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    return GammaMarket(
        market_id=mid,
        event_id=str(m.get("conditionId", "") or m.get("event_id", "")),
        event_slug=str(m.get("slug", "") or ""),
        event_title=str(m.get("groupItemTitle", "") or m.get("question", "")),
        question=str(m.get("question", "")),
        category=str(m.get("category", "") or ""),
        yes_price=yes_price,
        no_price=no_price,
        spread=spread,
        volume=float(m.get("volume", 0) or 0),
        liquidity=float(m.get("liquidity", 0) or m.get("liquidityNum", 0) or 0),
        volume_24hr=float(m.get("volume24hr", 0) or 0),
        volume_7d=float(m.get("volume1wk", 0) or m.get("volume7d", 0) or 0),
        price_change_7d=float(m.get("oneWeekPriceChange", 0) or m.get("priceChange7d", 0) or 0),
        price_change_1d=float(m.get("oneDayPriceChange", 0) or m.get("priceChange24hr", 0) or 0),
        end_date=end_date,
        active=bool(m.get("active", True)),
        closed=bool(m.get("closed", False)),
        group_item_title=str(m.get("groupItemTitle", "") or ""),
    )

async def _fetch_paginated(
    url: str,
    limit: int,
    pages: int,
    extra_params: Optional[dict] = None,
) -> list:
    """Generic paginated fetch from Gamma API with 0.3s delay between pages."""
    results: list = []
    async with httpx.AsyncClient(timeout=15.0, trust_env=True) as client:
        for page in range(pages):
            try:
                params = {"limit": limit, "offset": page * limit, **(extra_params or {})}
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not data:
                    break
                results.extend(data)
                await asyncio.sleep(0.3)  # 0.3s delay between pages
            except Exception as e:
                body_preview = ""
                try:
                    body_preview = (resp.text or "")[:160] if 'resp' in locals() else ""
                except Exception:
                    body_preview = ""

                if '503' in str(e) and ('Página bloqueada' in body_preview or '<html' in body_preview.lower()):
                    logger.warning(
                        "Gamma API bloqueada pela rede/origem (HTTP 503 + página de bloqueio). "
                        "Troque de rede (VPN/4G/outro IP) ou configure proxy HTTP(S)."
                    )
                else:
                    logger.warning("Gamma API fetch page %d failed: %s", page, e)
                break
    return results

async def fetch_gamma_markets(limit: int = 200, pages: int = 4) -> List[GammaMarket]:
    """Fetch active markets from Gamma API with pagination."""
    seen_ids: set = set()
    raw = await _fetch_paginated(
        f"{GAMMA_API}/markets", limit, pages,
        {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false"},
    )
    markets: List[GammaMarket] = []
    for m in raw:
        gm = _parse_market(m)
        if gm and gm.market_id not in seen_ids:
            seen_ids.add(gm.market_id)
            markets.append(gm)
    logger.info("Fetched %d active Gamma markets", len(markets))
    return markets

async def fetch_gamma_events(limit: int = 100, pages: int = 3) -> List[Dict[str, Any]]:
    """Fetch events (groups of related markets) for arbitrage detection."""
    return await _fetch_paginated(
        f"{GAMMA_API}/events", limit, pages,
        {"active": "true", "closed": "false"},
    )

# ---------------------------------------------------------------------------
# Strategy 1: Arbitrage — mutually exclusive markets summing > 100%
# ---------------------------------------------------------------------------

async def detect_arbitrage(events: List[Dict]) -> List[Signal]:
    """Detect arbitrage opportunities in multi-outcome events.

    Sums yes-prices across sub-markets; if sum > 1 + ARB_MIN_PROFIT_PCT
    (and < 1 + ARB_MAX_PROFIT_PCT), the event is flagged. Keyword
    heuristics filter out cumulative/threshold markets that are NOT
    mutually exclusive.
    """
    signals: List[Signal] = []

    for event in events:
        try:
            event_slug = event.get("slug", "")
            event_title = event.get("title", "")
            sub_markets = event.get("markets", [])

            active = [
                m for m in sub_markets
                if not m.get("closed", False) and m.get("active", True)
            ]
            if len(active) < ARB_MIN_ACTIVE_MARKETS:
                continue

            # Parse prices
            parsed: List[dict] = []
            for m in active:
                prices_raw = m.get("outcomePrices", "")
                if not prices_raw:
                    continue
                try:
                    p = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                    if isinstance(p, list) and len(p) >= 1:
                        parsed.append({
                            "id": str(m.get("id", "")),
                            "title": m.get("groupItemTitle", ""),
                            "yes_price": float(p[0]),
                            "question": m.get("question", ""),
                            "volume": float(m.get("volume", 0) or 0),
                        })
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue

            if len(parsed) < 2:
                continue

            # Keyword heuristics: skip non-exclusive markets
            titles = [p["title"] for p in parsed if p["title"]]
            if _is_non_exclusive(titles, event_title):
                continue

            total_prob = sum(p["yes_price"] for p in parsed)
            if total_prob <= 1.0 + ARB_MIN_PROFIT_PCT or total_prob > 1.0 + ARB_MAX_PROFIT_PCT:
                continue

            arb_profit = total_prob - 1.0
            total_vol = sum(p["volume"] for p in parsed)
            confidence = min(0.9, 0.4 + arb_profit * 5 + min(total_vol / 10000, 0.3))
            kelly = min(arb_profit / 0.5, 0.10) * ADVANCED_KELLY_FRACTION
            size = min(kelly * INITIAL_BANKROLL, ADVANCED_MAX_TRADE_SIZE)

            market_ids = [p["id"] for p in parsed]
            names = [f"{p['title']}@{p['yes_price']:.0%}" for p in parsed]

            signals.append(Signal(
                strategy="arbitrage",
                market_id=market_ids[0] if market_ids else "",
                event_slug=event_slug,
                event_title=event_title,
                direction="sell_all",
                model_probability=1.0 / len(parsed),
                market_probability=total_prob / len(parsed),
                edge=arb_profit,
                confidence=confidence,
                kelly_fraction=kelly,
                suggested_size=size,
                reasoning=f"ARB {arb_profit:.1%}: Σ={total_prob:.1%} | {', '.join(names)}",
                sources=["gamma_api_events"],
                arb_profit_pct=arb_profit * 100,
                arb_markets=market_ids,
            ))
        except Exception as e:
            logger.debug("Arbitrage detection failed for event: %s", e)

    signals.sort(key=lambda s: s.arb_profit_pct, reverse=True)
    logger.info("Arbitrage: %d opportunities found", len(signals))
    return signals

# ---------------------------------------------------------------------------
# Strategy 2: Mean Reversion — fade big 7d/1d moves
# ---------------------------------------------------------------------------

async def detect_mean_reversion(markets: List[GammaMarket]) -> List[Signal]:
    """Detect mean reversion opportunities from overreactions.

    - Uses 7d price change with fallback to 1d
    - Fades the direction of the move (30% reversion estimate)
    - Skips same-day markets (slug date check + <24h end_date)
    - Price range filter: 0.08 – 0.92
    - Edge cap: min(abs(price_change) * 0.4, 0.15)
    """
    signals: List[Signal] = []

    for m in markets:
        try:
            # Pick price change: 7d preferred, fallback 1d
            price_change = m.price_change_7d
            threshold = MEAN_REVERSION_THRESHOLD
            change_label = "7d"

            if MEAN_REVERSION_USE_1D and abs(m.price_change_7d) < 0.001:
                price_change = m.price_change_1d
                threshold = MEAN_REVERSION_1D_THRESHOLD
                change_label = "1d"

            if abs(price_change) < threshold:
                continue

            if m.yes_price < 0.08 or m.yes_price > 0.92:
                continue

            # Skip same-day markets
            if m.end_date:
                hours_left = (m.end_date - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < 24:
                    continue
            slug = (m.event_slug or "").lower()
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            if today_str in slug or yesterday_str in slug:
                continue
            if m.volume_24hr < 1000:
                continue

            # Direction: fade the move
            if price_change > 0:
                direction = "no"
                reversion = price_change * 0.3
                model_prob = max(0.1, m.yes_price - reversion)
                edge = m.yes_price - model_prob
            else:
                direction = "yes"
                reversion = abs(price_change) * 0.3
                model_prob = min(0.9, m.yes_price + reversion)
                edge = model_prob - m.yes_price

            # Edge cap
            edge = min(edge, min(abs(price_change) * 0.4, 0.15))
            if edge < ADVANCED_MIN_EDGE:
                continue

            extremeness = max(m.yes_price - 0.5, 0.5 - m.yes_price) / 0.5
            confidence = min(0.8, 0.4 + abs(price_change) * 2) * (1.0 - extremeness * 0.5)

            win_prob = model_prob if direction == "yes" else 1 - model_prob
            entry_price = m.yes_price if direction == "yes" else m.no_price
            kelly_frac, size = kelly_size(win_prob, entry_price, max_fraction=0.05)

            signals.append(Signal(
                strategy="mean_reversion",
                market_id=m.market_id,
                event_slug=_slug_or_question(m),
                event_title=_title_or_question(m),
                direction=direction,
                model_probability=model_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                suggested_size=size,
                reasoning=(
                    f"MEAN_REVERT [{change_label}]: {m.question[:55]} | "
                    f"{change_label} move:{price_change:+.1%} Mkt:{m.yes_price:.0%} "
                    f"Model:{model_prob:.0%} Edge:{edge:.1%}"
                ),
                sources=["gamma_api_markets", "mean_reversion"],
            ))
        except Exception as e:
            logger.debug("Mean reversion failed for market %s: %s", m.market_id, e)

    signals.sort(key=lambda s: abs(s.edge), reverse=True)
    logger.info("Mean Reversion: %d signals found", len(signals))
    return signals

# ---------------------------------------------------------------------------
# Strategy 3: Value Betting — high vol near 50%, low liq+high vol, contrarian
# ---------------------------------------------------------------------------

async def detect_value_betting(markets: List[GammaMarket]) -> List[Signal]:
    """Detect value betting opportunities.

    Three sub-patterns:
      A) Near 50% with high volume — slight directional lean
      B) Low liquidity + high volume — informed money signal
      C) Contrarian divergence — fade extreme moves
    """
    signals: List[Signal] = []

    for m in markets:
        try:
            if m.volume_24hr < VALUE_MIN_VOLUME:
                continue
            if m.yes_price < 0.08 or m.yes_price > 0.92:
                continue
            if m.liquidity < 10000:
                continue

            divergence = abs(m.yes_price - 0.5)
            vol_liq_ratio = m.volume_24hr / max(m.liquidity, 1)
            signal_type = None
            edge = 0.0
            model_prob = 0.5
            direction = "yes"
            confidence = 0.5

            # Pattern A: Near 50% with high volume
            if divergence < 0.15 and m.volume_24hr > VALUE_MIN_VOLUME * 2:
                if vol_liq_ratio > 1.5:
                    direction = "yes" if m.yes_price > 0.5 else "no"
                    model_prob = m.yes_price + (0.05 if m.yes_price > 0.5 else -0.05)
                    edge = abs(model_prob - m.yes_price)
                    signal_type = "high_volume_near_50"
                    confidence = 0.5 + min(vol_liq_ratio / 10, 0.3)

            # Pattern B: Low liquidity + high volume
            if m.liquidity < m.volume_24hr * 0.5 and m.volume_24hr > VALUE_MIN_VOLUME:
                direction = "yes" if m.yes_price > m.no_price else "no"
                model_prob = m.yes_price + (0.08 if direction == "yes" else -0.08)
                model_prob = max(0.01, min(0.99, model_prob))
                edge = abs(model_prob - m.yes_price)
                signal_type = "informed_money_low_liq"
                confidence = 0.4 + min(vol_liq_ratio / 5, 0.35)

            # Pattern C: Contrarian divergence
            if divergence >= VALUE_DIVERGENCE_THRESHOLD and m.volume_24hr > VALUE_MIN_VOLUME * 5:
                direction = "no" if m.yes_price > 0.65 else "yes"
                model_prob = m.yes_price + (-0.05 if m.yes_price > 0.65 else 0.05)
                edge = abs(model_prob - m.yes_price)
                signal_type = "contrarian_divergence"
                confidence = 0.4 + divergence

            if not signal_type or edge < ADVANCED_MIN_EDGE:
                continue

            win_prob = model_prob if direction == "yes" else 1 - model_prob
            entry_price = m.yes_price if direction == "yes" else m.no_price
            kelly_frac, size = kelly_size(win_prob, entry_price, max_fraction=0.05)

            signals.append(Signal(
                strategy="value",
                market_id=m.market_id,
                event_slug=_slug_or_question(m),
                event_title=_title_or_question(m),
                direction=direction,
                model_probability=model_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=min(confidence, 0.85),
                kelly_fraction=kelly_frac,
                suggested_size=size,
                reasoning=(
                    f"VALUE [{signal_type}]: {m.question[:60]} | "
                    f"Mkt:{m.yes_price:.0%} Model:{model_prob:.0%} Edge:{edge:.1%} "
                    f"Vol24h:${m.volume_24hr:,.0f} Liq:${m.liquidity:,.0f}"
                ),
                sources=["gamma_api_markets", f"value_{signal_type}"],
            ))
        except Exception as e:
            logger.debug("Value detection failed for market %s: %s", m.market_id, e)

    signals.sort(key=lambda s: s.edge, reverse=True)
    logger.info("Value Betting: %d signals found", len(signals))
    return signals

# ---------------------------------------------------------------------------
# Strategy 4: Volume Spikes — vol/liq>3x or vol/cat_avg>3x
# ---------------------------------------------------------------------------

async def detect_volume_spikes(markets: List[GammaMarket]) -> List[Signal]:
    """Detect unusual volume activity relative to liquidity.

    Triggers when:
      - vol/liq ratio > VOLUME_SPIKE_MULTIPLIER, OR
      - vol/category_avg > VOLUME_SPIKE_MULTIPLIER
    Direction from 1d price change; edge from price move magnitude.
    """
    signals: List[Signal] = []
    cat_avg = category_avg_volumes(markets)

    for m in markets:
        try:
            if m.volume_24hr < VOLUME_SPIKE_MIN_VOL:
                continue
            if m.yes_price < 0.05 or m.yes_price > 0.95:
                continue
            if m.liquidity < 10000:
                continue

            vol_liq_ratio = m.volume_24hr / max(m.liquidity, 1)
            avg_cat_vol = cat_avg.get(m.category, 50000)
            vol_vs_category = m.volume_24hr / max(avg_cat_vol, 1)

            if vol_liq_ratio < VOLUME_SPIKE_MULTIPLIER and vol_vs_category < VOLUME_SPIKE_MULTIPLIER:
                continue

            direction = _direction_from_price_change(m)

            # Edge estimation
            move_edge = min(abs(m.price_change_1d) * 0.5, 0.10)
            if move_edge < 0.01 and m.yes_price < 0.20:
                move_edge = min(vol_vs_category / 40, 0.05)
            model_prob = (
                min(0.85, m.yes_price + move_edge) if direction == "yes"
                else max(0.15, m.yes_price - move_edge)
            )
            edge = abs(model_prob - m.yes_price)

            if edge < ADVANCED_MIN_EDGE:
                continue

            spike_type = "vol_liq" if vol_liq_ratio >= VOLUME_SPIKE_MULTIPLIER else "vs_category"
            multiplier = vol_liq_ratio if spike_type == "vol_liq" else vol_vs_category
            confidence = min(0.75, 0.3 + min(multiplier / 10, 0.45))

            win_prob = model_prob if direction == "yes" else 1 - model_prob
            entry_price = m.yes_price if direction == "yes" else m.no_price

            # Extra conservative for volume spikes
            if entry_price < 0.05 or entry_price > 0.95:
                kelly_frac = max(confidence * 0.01, 0.002)
                size = min(kelly_frac * INITIAL_BANKROLL, ADVANCED_MAX_TRADE_SIZE)
            else:
                kelly_frac, size = kelly_size(win_prob, entry_price, conservative=0.5, max_fraction=0.04)
                kelly_frac = max(kelly_frac, 0.002)

            signals.append(Signal(
                strategy="volume_spike",
                market_id=m.market_id,
                event_slug=_slug_or_question(m),
                event_title=_title_or_question(m),
                direction=direction,
                model_probability=model_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                suggested_size=size,
                reasoning=(
                    f"VOL_SPIKE [{spike_type}]: {m.question[:60]} | "
                    f"Vol/Liq:{vol_liq_ratio:.1f}x CatAvg:{vol_vs_category:.1f}x "
                    f"Vol24h:${m.volume_24hr:,.0f} Liq:${m.liquidity:,.0f}"
                ),
                sources=["gamma_api_markets", "volume_spike"],
            ))
        except Exception as e:
            logger.debug("Volume spike detection failed for market %s: %s", m.market_id, e)

    signals.sort(key=lambda s: s.confidence, reverse=True)
    logger.info("Volume Spikes: %d detected", len(signals))
    return signals

# ---------------------------------------------------------------------------
# Strategy 5: Smart Money — tight spread + high vol ratio
# ---------------------------------------------------------------------------

async def detect_smart_money(markets: List[GammaMarket]) -> List[Signal]:
    """Detect smart money presence via tight spreads and high volume.

    Filters:
      - spread < SMART_MONEY_MAX_SPREAD
      - yes_price in [0.08, 0.92]
      - volume_24hr > $50K, liquidity > SMART_MONEY_MIN_LIQUIDITY
      - vol_ratio (vs category avg) >= SMART_MONEY_VOLUME_RATIO
    """
    signals: List[Signal] = []
    cat_avg = category_avg_volumes(markets)

    for m in markets:
        try:
            if m.spread > SMART_MONEY_MAX_SPREAD:
                continue
            if m.yes_price < 0.08 or m.yes_price > 0.92:
                continue
            # Filter out multi-winner markets (World Cup, Eurovision, etc.)
            q_lower = m.question.lower()
            if any(kw in q_lower for kw in ["win the", "world cup", "eurovision", "championship winner"]):
                continue
            if m.volume_24hr < 50000:
                continue
            if m.liquidity < SMART_MONEY_MIN_LIQUIDITY:
                continue

            avg_cat_vol = cat_avg.get(m.category, 50000)
            vol_ratio = m.volume_24hr / max(avg_cat_vol, 1)
            if vol_ratio < SMART_MONEY_VOLUME_RATIO:
                continue

            smart_score = (
1 / max(m.spread, 0.001)) * vol_ratio

            # Direction from price change
            if m.price_change_1d > 0.01:
                direction = "yes"
            elif m.price_change_1d < -0.01:
                direction = "no"
            else:
                direction = "yes" if m.yes_price > 0.5 else "no"

            move_edge = min(abs(m.price_change_1d) * 0.5, 0.10)
            vol_edge = min(vol_ratio / 30, 0.05)
            raw_edge = move_edge + vol_edge
            model_prob = (
                min(0.90, m.yes_price + raw_edge) if direction == "yes"
                else max(0.10, m.yes_price - raw_edge)
            )
            edge = raw_edge

            if edge < ADVANCED_MIN_EDGE:
                continue

            confidence = min(0.8, 0.35 + min(smart_score / 50, 0.45))

            win_prob = model_prob if direction == "yes" else 1 - model_prob
            entry_price = m.yes_price if direction == "yes" else m.no_price
            if entry_price < 0.03 or entry_price > 0.97:
                kelly_frac = confidence * 0.01
                size = min(kelly_frac * INITIAL_BANKROLL, ADVANCED_MAX_TRADE_SIZE)
            else:
                kelly_frac, size = kelly_size(win_prob, entry_price, conservative=0.5, max_fraction=0.04)
                kelly_frac = max(kelly_frac, 0.002)

            signals.append(Signal(
                strategy="smart_money",
                market_id=m.market_id,
                event_slug=_slug_or_question(m),
                event_title=_title_or_question(m),
                direction=direction,
                model_probability=model_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                suggested_size=size,
                reasoning=(
                    f"SMART_MONEY: {m.question[:60]} | "
                    f"Spread:{m.spread:.3f} VolRatio:{vol_ratio:.1f}x "
                    f"Score:{smart_score:.1f} Vol24h:${m.volume_24hr:,.0f}"
                ),
                sources=["gamma_api_markets", "smart_money"],
            ))
        except Exception as e:
            logger.debug("Smart money detection failed for market %s: %s", m.market_id, e)

    signals.sort(key=lambda s: s.confidence, reverse=True)
    logger.info("Smart Money: %d signals found", len(signals))
    return signals

# ---------------------------------------------------------------------------
# Main scanner — orchestrates all strategies
# ---------------------------------------------------------------------------

async def scan_all() -> Dict[str, Any]:
    """Run all 5 strategies and return a summary dict.

    Returns:
        {
            "total_markets": int,
            "total_events": int,
            "total_signals": int,
            "actionable_signals": int,
            "signals": [Signal.to_dict(), ...],
            "by_strategy": {"arbitrage": int, ...},
            "timestamp": "ISO-8601",
        }
    """
    logger.info("=" * 50)
    logger.info("ADVANCED STRATEGY SCAN: Fetching markets...")

    # Fetch data
    try:
        markets = await fetch_gamma_markets()
    except Exception as e:
        logger.error("Failed to fetch Gamma markets: %s", e)
        markets = []

    try:
        events = await fetch_gamma_events()
    except Exception as e:
        logger.error("Failed to fetch Gamma events: %s", e)
        events = []

    total_markets = len(markets)
    logger.info("Fetched %d markets, %d events", total_markets, len(events))

    # Run strategies (each wrapped in try/except)
    all_signals: List[Signal] = []
    strategy_funcs: List[Tuple[str, Any]] = [
        ("Arbitrage", lambda: detect_arbitrage(events)),
        ("Value", lambda: detect_value_betting(markets)),
        ("Mean Reversion", lambda: detect_mean_reversion(markets)),
        ("Volume Spike", lambda: detect_volume_spikes(markets)),
        ("Smart Money", lambda: detect_smart_money
(markets)),
    ]
    for name, fn in strategy_funcs:
        try:
            all_signals.extend(await fn())
        except Exception as e:
            logger.error("%s strategy failed: %s", name, e)

    # Sort by confidence * |edge|
    all_signals.sort(key=lambda s: s.confidence * abs(s.edge), reverse=True)
    actionable = [s for s in all_signals if s.passes_threshold]

    logger.info(
        "SCAN COMPLETE: %d total signals, %d actionable",
        len(all_signals), len(actionable),
    )
    logger.info("=" * 50)

    # Build by_strategy counts
    by_strategy: Dict[str, int] = defaultdict(int)
    for s in all_signals:
        by_strategy[s.strategy] += 1

    return {
        "total_markets": total_markets,
        "total_events": len(events),
        "total_signals": len(all_signals),
        "actionable_signals": len(actionable),
        "signals": [s.to_dict() for s in all_signals],
        "by_strategy": dict(by_strategy),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    async def _main() -> None:
        print("Running Polymarket advanced strategy scanner...")
        result = await scan_all()

        print(f"\nScanned {result['total_markets']} markets, {result['total_events']} events")
        print(f"Total signals: {result['total_signals']}, Actionable: {result['actionable_signals']}")
        print(f"By strategy: {result['by_strategy']}")

        # Show top 3 per strategy
        by_strat: Dict[str, list] = defaultdict(list)
        for s in result["signals"]:
            by_strat[s["strategy"]].append(s)

        for strat, sigs in by_strat.items():
            print(f"\n--- {strat}: {len(sigs)} signals ---")
            for s in sigs[:3]:
                print(f"  {s['event_title']}: edge={s['edge']:.1%} conf={s['confidence']:.0%}")
                print(f"    {s['reasoning'][:120]}")

    asyncio.run(_main())

