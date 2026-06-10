"""Standalone async scanner — fetches Polymarket Gamma API and runs multiple strategies.

Strategies:
  1. detect_arbitrage      — mutually-exclusive markets summing > 1 + min_profit
  2. detect_mean_reversion — fade big 7d moves, skip same-day, 0.08-0.92 range
  3. detect_value_betting  — high vol near 50%, low liq+high vol, contrarian
  4. detect_volume_spikes  — vol/liq>3x or vol/cat_avg>3x
  5. detect_smart_money    — tight spread + high vol ratio
  6. detect_event_countdown — markets near end-date with directional bias
  7. detect_endgame_last_minute — BTC 5m last-minute directional sniper

Usage:
  python scanner.py                # run scan_all and print summary
  from scanner import scan_all     # import as library
"""
import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from common import install_polymarket_dns_fallback, yes_no_from_gamma_market as _yes_no_from_gamma_market

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

install_polymarket_dns_fallback()

# Arbitrage
ARB_MIN_PROFIT_PCT = 0.01   # 1% minimum arb profit
ARB_MAX_PROFIT_PCT = 0.15   # 15% max — beyond this, almost certainly not mutually exclusive
ARB_MIN_ACTIVE_MARKETS = 2

# General
ADVANCED_MIN_EDGE = 0.05    # 5% minimum edge to be actionable
ADVANCED_KELLY_FRACTION = 0.25  # quarter-Kelly
INITIAL_BANKROLL = 10000.0
ADVANCED_MAX_TRADE_SIZE = 50.0
GAMMA_TIMEOUT_SECONDS = float(os.getenv("PAPER_GAMMA_TIMEOUT_SECONDS", "8"))
GAMMA_RETRY_ATTEMPTS = int(os.getenv("PAPER_GAMMA_RETRY_ATTEMPTS", "2"))

# BTC 5-minute momentum/scalping, inspired by @ndjjwobaq public profile.
# It enters only the current/near-current BTC Up/Down 5m market, usually
# after the interval has started, and follows the side implied by live odds.
BTC_5M_ENABLED = os.getenv("PAPER_BTC_5M_ENABLED", "1") == "1"
BTC_5M_BASE_SIZE = float(os.getenv("PAPER_BTC_5M_BASE_SIZE", "50"))
BTC_5M_MIN_SECONDS_IN = int(os.getenv("PAPER_BTC_5M_MIN_SECONDS_IN", "45"))
BTC_5M_MAX_SECONDS_IN = int(os.getenv("PAPER_BTC_5M_MAX_SECONDS_IN", "305"))
BTC_5M_MIN_DIRECTIONAL_EDGE = float(os.getenv("PAPER_BTC_5M_MIN_DIRECTIONAL_EDGE", "0.035"))
BTC_5M_NORMAL_MAX_PRICE = float(os.getenv("PAPER_BTC_5M_NORMAL_MAX_PRICE", "0.72"))
BTC_5M_LATE_CONFIRM_MIN_SECONDS = int(os.getenv("PAPER_BTC_5M_LATE_CONFIRM_MIN_SECONDS", "210"))
BTC_5M_LATE_CONFIRM_MAX_PRICE = float(os.getenv("PAPER_BTC_5M_LATE_CONFIRM_MAX_PRICE", "0.88"))
BTC_5M_MIN_LIQUIDITY = float(os.getenv("PAPER_BTC_5M_MIN_LIQUIDITY", "1000"))
BTC_5M_HIGH_PRICE_PENALTY_START = float(os.getenv("PAPER_BTC_5M_HIGH_PRICE_PENALTY_START", "0.78"))
BTC_5M_HIGH_PRICE_MAX_PENALTY = float(os.getenv("PAPER_BTC_5M_HIGH_PRICE_MAX_PENALTY", "0.045"))
BTC_KLINES_API = os.getenv("PAPER_BTC_KLINES_API", "https://api.binance.com/api/v3/klines")
BTC_KLINES_TIMEOUT_SECONDS = float(os.getenv("PAPER_BTC_KLINES_TIMEOUT_SECONDS", "5"))
# CLOB midpoint: Gamma outcomePrices lag badly on 5-minute markets (observed
# 0.855 vs 0.985 real at the same instant), so fast strategies must price off
# the live orderbook midpoint instead.
CLOB_API = os.getenv("PAPER_POLYMARKET_CLOB_API", "https://clob.polymarket.com")
CLOB_TIMEOUT_SECONDS = float(os.getenv("PAPER_CLOB_TIMEOUT_SECONDS", "5"))
# Daily-temperature events tag: these markets rarely reach the volume-ordered
# top pages (~10 visible vs ~1100 active), so the weather strategy fetches
# them by tag instead of relying on the general volume-sorted universe.
WEATHER_TAG_ID = os.getenv("PAPER_WEATHER_TAG_ID", "103040")

# BTC 5m Endgame (last-minute dedicated)
ENDGAME_ENABLED = os.getenv("PAPER_ENDGAME_ENABLED", "1") == "1"
ENDGAME_WINDOW_START_SECONDS = int(os.getenv("PAPER_ENDGAME_WINDOW_START_SECONDS", "240"))
ENDGAME_WINDOW_END_SECONDS = int(os.getenv("PAPER_ENDGAME_WINDOW_END_SECONDS", "299"))
ENDGAME_MIN_DIRECTIONAL_EDGE = float(os.getenv("PAPER_ENDGAME_MIN_DIRECTIONAL_EDGE", "0.06"))
ENDGAME_MIN_LIQUIDITY = float(os.getenv("PAPER_ENDGAME_MIN_LIQUIDITY", "1500"))
ENDGAME_MAX_ENTRY_PRICE = float(os.getenv("PAPER_ENDGAME_MAX_ENTRY_PRICE", "0.90"))
ENDGAME_BASE_SIZE = float(os.getenv("PAPER_ENDGAME_BASE_SIZE", "35"))

# Weather forecast strategy (shadow-first by default in execution policy)
WEATHER_ENABLED = os.getenv("PAPER_WEATHER_ENABLED", "1") == "1"
WEATHER_MIN_EDGE = float(os.getenv("PAPER_WEATHER_MIN_EDGE", "0.08"))
WEATHER_MAX_SPREAD = float(os.getenv("PAPER_WEATHER_MAX_SPREAD", "0.08"))
WEATHER_MIN_LIQUIDITY = float(os.getenv("PAPER_WEATHER_MIN_LIQUIDITY", "500"))
WEATHER_MAX_DAYS_AHEAD = int(os.getenv("PAPER_WEATHER_MAX_DAYS_AHEAD", "16"))
WEATHER_TIMEOUT_SECONDS = float(os.getenv("PAPER_WEATHER_TIMEOUT_SECONDS", "8"))
WEATHER_GEOCODE_API = os.getenv("PAPER_WEATHER_GEOCODE_API", "https://geocoding-api.open-meteo.com/v1/search")
WEATHER_FORECAST_API = os.getenv("PAPER_WEATHER_FORECAST_API", "https://api.open-meteo.com/v1/forecast")
WEATHER_ENSEMBLE_API = os.getenv("PAPER_WEATHER_ENSEMBLE_API", "https://ensemble-api.open-meteo.com/v1/ensemble")

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

# Smart Money Copy-Trading (shadow-only — see strategy docstring)
DATA_API = os.getenv("PAPER_POLYMARKET_DATA_API", "https://data-api.polymarket.com")
SMART_MONEY_COPY_WALLETS = [w.strip() for w in os.getenv("PAPER_SMART_MONEY_WALLETS", "").split(",") if w.strip()]
SMART_MONEY_COPY_TIMEOUT_SECONDS = float(os.getenv("PAPER_SMART_MONEY_COPY_TIMEOUT_SECONDS", "8"))
SMART_MONEY_COPY_MIN_WIN_RATE = float(os.getenv("PAPER_SMART_MONEY_COPY_MIN_WIN_RATE", "0.60"))
SMART_MONEY_COPY_MIN_PROFIT_FACTOR = float(os.getenv("PAPER_SMART_MONEY_COPY_MIN_PROFIT_FACTOR", "1.5"))
SMART_MONEY_COPY_MAX_CONCENTRATION = float(os.getenv("PAPER_SMART_MONEY_COPY_MAX_CONCENTRATION", "0.30"))

# Event Countdown
EVENT_COUNTDOWN_MIN_HOURS = float(os.getenv("PAPER_EVENT_COUNTDOWN_MIN_HOURS", "0.25"))
EVENT_COUNTDOWN_MAX_HOURS = float(os.getenv("PAPER_EVENT_COUNTDOWN_MAX_HOURS", "6"))
EVENT_COUNTDOWN_MIN_LIQUIDITY = float(os.getenv("PAPER_EVENT_COUNTDOWN_MIN_LIQUIDITY", "15000"))
EVENT_COUNTDOWN_MIN_VOL24H = float(os.getenv("PAPER_EVENT_COUNTDOWN_MIN_VOL24H", "25000"))
EVENT_COUNTDOWN_MID_BAND = float(os.getenv("PAPER_EVENT_COUNTDOWN_MID_BAND", "0.07"))
EVENT_COUNTDOWN_MAX_MODEL_BOOST = float(os.getenv("PAPER_EVENT_COUNTDOWN_MAX_MODEL_BOOST", "0.08"))

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
    game_start_time: datetime | None = None
    condition_id: str = ""
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
    spread: float = 0.0
    liquidity: float = 0.0
    volume_24hr: float = 0.0
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
            "spread": round(self.spread, 4),
            "liquidity": round(self.liquidity, 2),
            "volume_24hr": round(self.volume_24hr, 2),
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

def _high_entry_price_penalty(entry_price: float) -> float:
    """Penalty for short-horizon BTC entries where remaining upside is small."""
    if entry_price <= BTC_5M_HIGH_PRICE_PENALTY_START:
        return 0.0
    room = max(0.001, 1.0 - BTC_5M_HIGH_PRICE_PENALTY_START)
    expensive = min(1.0, (entry_price - BTC_5M_HIGH_PRICE_PENALTY_START) / room)
    return BTC_5M_HIGH_PRICE_MAX_PENALTY * expensive


_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _weather_text(m: GammaMarket) -> str:
    return " ".join(x for x in [m.question, m.event_title, m.event_slug, m.group_item_title] if x)


def _parse_weather_date(text: str, now: Optional[datetime] = None) -> Optional[str]:
    now = now or datetime.now(timezone.utc)
    iso_match = re.search(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", text)
    if iso_match:
        year, month, day = map(int, iso_match.groups())
        try:
            return datetime(year, month, day, tzinfo=timezone.utc).date().isoformat()
        except ValueError:
            return None

    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    patterns = [
        rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,\s*(20\d{{2}}))?\b",
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\.?(?:\s+(20\d{{2}}))?\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            continue
        parts = m.groups()
        if parts[0].isdigit():
            day = int(parts[0])
            month = _MONTHS[parts[1].lower().rstrip(".")]
            year = int(parts[2]) if parts[2] else now.year
        else:
            month = _MONTHS[parts[0].lower().rstrip(".")]
            day = int(parts[1])
            year = int(parts[2]) if parts[2] else now.year
        try:
            dt = datetime(year, month, day, tzinfo=timezone.utc)
        except ValueError:
            return None
        if dt.date() < (now - timedelta(days=1)).date() and not parts[2]:
            dt = datetime(year + 1, month, day, tzinfo=timezone.utc)
        return dt.date().isoformat()
    return None


def _parse_weather_location(text: str) -> Optional[str]:
    clean = text.replace("-", " ")
    patterns = [
        r"\bin\s+([A-Za-z][A-Za-z .'-]{2,60}?)\s+(?:on|by|before|after|during)\b",
        r"\bfor\s+([A-Za-z][A-Za-z .'-]{2,60}?)\s+(?:on|by|before|after|during)\b",
        r"\bat\s+([A-Za-z][A-Za-z .'-]{2,60}?)\s+(?:on|by|before|after|during)\b",
        r"^([A-Za-z][A-Za-z .'-]{1,45}?)\s+(?:high|low|max|min|temperature|temp)\b",
    ]
    stop_words = {
        "the", "a", "an", "will", "it", "rain", "snow", "temperature", "high",
        "low", "above", "below", "over", "under", "degrees", "fahrenheit", "celsius",
    }
    for pattern in patterns:
        m = re.search(pattern, clean, flags=re.IGNORECASE)
        if not m:
            continue
        loc = re.sub(r"\s+", " ", m.group(1)).strip(" ?.,")
        loc = re.sub(r"\b(?:high|low|temperature|rain|snow|weather)\b", "", loc, flags=re.IGNORECASE)
        loc = re.sub(r"\s+", " ", loc).strip(" ?.,")
        if len(loc) >= 3 and loc.lower() not in stop_words:
            return loc
    return None


def _parse_weather_metric(text: str) -> Optional[Dict[str, Any]]:
    lower = text.lower()
    if any(k in lower for k in ["rain", "precipitation", "precipitate", "showers"]):
        return {"metric": "rain"}

    if not any(k in lower for k in ["temperature", "temp", "degrees", "°", "fahrenheit", "celsius"]):
        return None

    temp_kind = "min" if re.search(r"\b(low|lowest|min|minimum)\b", lower) else "max"

    def _band_spec(low: float | None, high: float | None, unit_raw: str) -> dict[str, Any]:
        # Official readings are integers, so a bucket like "27°C" resolves YES
        # for any continuous value in [26.5, 27.5) — hence the ±0.5 edges.
        unit = "celsius" if unit_raw.startswith("c") else "fahrenheit"
        return {
            "metric": "temperature",
            "operator": "band",
            "band_low": low - 0.5 if low is not None else None,
            "band_high": high + 0.5 if high is not None else None,
            "unit": unit,
            "temp_kind": temp_kind,
        }

    # Exact-bucket markets ("be 27°C", "between 80-81°F", "33°C or higher"):
    # the dominant phrasing on Polymarket daily-temperature markets.
    between = re.search(
        r"between\s+(-?\d+(?:\.\d+)?)\s*(?:°\s*([cf]))?\s*(?:-|–|to|and)\s*(-?\d+(?:\.\d+)?)\s*°\s*([cf])\b",
        lower,
    )
    if between:
        lo, hi = float(between.group(1)), float(between.group(3))
        return _band_spec(min(lo, hi), max(lo, hi), between.group(4) or between.group(2) or "f")
    or_higher = re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*([cf])\s*or\s+(?:higher|above|more|warmer)\b", lower)
    if or_higher:
        return _band_spec(float(or_higher.group(1)), None, or_higher.group(2))
    or_lower = re.search(r"(-?\d+(?:\.\d+)?)\s*°\s*([cf])\s*or\s+(?:lower|below|less|colder)\b", lower)
    if or_lower:
        return _band_spec(None, float(or_lower.group(1)), or_lower.group(2))
    exact = re.search(r"\bbe\s+(-?\d+(?:\.\d+)?)\s*°\s*([cf])\b", lower)
    if exact:
        value = float(exact.group(1))
        return _band_spec(value, value, exact.group(2))

    op_match = re.search(
        r"\b(above|over|at least|exceed(?:s)?|greater than|below|under|less than|at most)\b[^0-9-]*(-?\d+(?:\.\d+)?)\s*(?:°?\s*([fc])|degrees?\s*(fahrenheit|celsius)?)?",
        lower,
    )
    if not op_match:
        return None
    op_raw = op_match.group(1)
    operator = "above" if op_raw in {"above", "over", "at least", "exceed", "exceeds", "greater than"} else "below"
    threshold = float(op_match.group(2))
    unit_raw = (op_match.group(3) or op_match.group(4) or "f").lower()
    unit = "celsius" if unit_raw.startswith("c") else "fahrenheit"
    return {
        "metric": "temperature",
        "operator": operator,
        "threshold": threshold,
        "unit": unit,
        "temp_kind": temp_kind,
    }


def _is_weather_market(m: GammaMarket) -> bool:
    text = _weather_text(m).lower()
    weather_terms = [
        "weather", "rain", "precipitation", "showers", "temperature", "degrees",
        "fahrenheit", "celsius", "heat", "cold", "snow",
    ]
    return any(term in text for term in weather_terms)


def _weather_probability_from_forecast(spec: Dict[str, Any], forecast: Dict[str, Any], date_iso: str) -> Optional[Tuple[float, float, str]]:
    daily = forecast.get("daily") if isinstance(forecast, dict) else None
    if not isinstance(daily, dict):
        return None
    dates = list(daily.get("time") or [])
    if date_iso not in dates:
        return None
    i = dates.index(date_iso)

    if spec["metric"] == "rain":
        probs = daily.get("precipitation_probability_max") or []
        sums = daily.get("precipitation_sum") or []
        precip_prob = float(probs[i] or 0.0) / 100.0 if i < len(probs) and probs[i] is not None else 0.0
        precip_sum = float(sums[i] or 0.0) if i < len(sums) and sums[i] is not None else 0.0
        # Rain markets usually mean measurable rain. Combine probability and
        # forecast amount without pretending to have station-level certainty.
        amount_boost = min(0.25, precip_sum / 10.0)
        yes_prob = max(0.03, min(0.97, precip_prob * 0.85 + amount_boost))
        return yes_prob, precip_sum, f"rain_prob={precip_prob:.0%} precip_sum={precip_sum:.1f}mm"

    if spec.get("operator") == "band":
        # A single point forecast can't calibrate a 1-degree bucket probability;
        # band markets are ensemble-only.
        return None

    key = "temperature_2m_min" if spec.get("temp_kind") == "min" else "temperature_2m_max"
    temps = daily.get(key) or []
    if i >= len(temps) or temps[i] is None:
        return None
    forecast_temp = float(temps[i])
    threshold = float(spec["threshold"])
    margin = 3.0 if spec.get("unit") == "fahrenheit" else 1.7
    diff = forecast_temp - threshold
    if spec.get("operator") == "below":
        diff = -diff
    if abs(diff) < margin:
        return None
    yes_prob = 0.5 + max(-0.42, min(0.42, diff / (margin * 4.0)))
    return yes_prob, forecast_temp, f"{key}={forecast_temp:.1f} threshold={threshold:.1f} {spec.get('unit')}"


def _ensemble_member_keys(hourly: Dict[str, Any], base: str) -> List[str]:
    """Return sorted hourly keys for each ensemble member of a given base variable.

    Open-Meteo names ensemble members as '<base>_member01', '<base>_member02', ...
    (and sometimes a bare '<base>' for the control run, which we skip to keep a
    consistent member set across variables).
    """
    prefix = f"{base}_member"
    return sorted(k for k in hourly if k.startswith(prefix))


def _weather_probability_from_ensemble(spec: Dict[str, Any], ensemble: Dict[str, Any], date_iso: str) -> Optional[Tuple[float, float, str]]:
    hourly = ensemble.get("hourly") if isinstance(ensemble, dict) else None
    if not isinstance(hourly, dict):
        return None
    times = list(hourly.get("time") or [])
    day_idx = [i for i, t in enumerate(times) if isinstance(t, str) and t.startswith(date_iso)]
    if not day_idx:
        return None

    if spec["metric"] == "rain":
        member_keys = _ensemble_member_keys(hourly, "precipitation")
        if not member_keys:
            return None
        sums = []
        for key in member_keys:
            values = hourly.get(key) or []
            day_values = [float(values[i]) for i in day_idx if i < len(values) and values[i] is not None]
            if day_values:
                sums.append(sum(day_values))
        if not sums:
            return None
        threshold_mm = 0.5  # measurable rain
        hits = sum(1 for s in sums if s >= threshold_mm)
        yes_prob = max(0.04, min(0.96, hits / len(sums)))
        median_sum = sorted(sums)[len(sums) // 2]
        return yes_prob, median_sum, f"ensemble_rain members={len(sums)} hits={hits} median_sum={median_sum:.1f}mm"

    base = "temperature_2m"
    member_keys = _ensemble_member_keys(hourly, base)
    if not member_keys:
        return None
    operator = spec.get("operator")
    temp_kind = spec.get("temp_kind")
    aggregates = []
    for key in member_keys:
        values = hourly.get(key) or []
        day_values = [float(values[i]) for i in day_idx if i < len(values) and values[i] is not None]
        if not day_values:
            continue
        aggregates.append(min(day_values) if temp_kind == "min" else max(day_values))
    if not aggregates:
        return None

    if operator == "band":
        lo, hi = spec.get("band_low"), spec.get("band_high")

        def _meets(value: float) -> bool:
            return (lo is None or value >= lo) and (hi is None or value < hi)

        bounds_label = f"band=[{lo if lo is not None else '-inf'},{hi if hi is not None else '+inf'})"
    else:
        threshold = float(spec["threshold"])

        def _meets(value: float) -> bool:
            return value < threshold if operator == "below" else value > threshold

        bounds_label = f"threshold={threshold:.1f}"

    hits = sum(1 for v in aggregates if _meets(v))
    yes_prob = max(0.04, min(0.96, hits / len(aggregates)))
    median_value = sorted(aggregates)[len(aggregates) // 2]
    return yes_prob, median_value, (
        f"ensemble_{temp_kind or 'max'} members={len(aggregates)} hits={hits} "
        f"median={median_value:.1f} {bounds_label} {spec.get('unit')}"
    )

# ---------------------------------------------------------------------------
# Market fetcher
# ---------------------------------------------------------------------------

def _parse_market(m: dict) -> Optional[GammaMarket]:
    """Parse a single Gamma API market dict into GammaMarket."""
    mid = str(m.get("id", ""))
    if not mid:
        return None

    yes_price, no_price = _yes_no_from_gamma_market(m)

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

    # Game start time (sports markets only; absent for non-sports markets).
    # Used to detect live/in-play games where 24h price momentum is stale.
    game_start_time = None
    game_start_str = m.get("gameStartTime") or m.get("eventStartTime") or ""
    if game_start_str:
        try:
            game_start_time = datetime.fromisoformat(str(game_start_str).replace("Z", "+00:00"))
            if game_start_time.tzinfo is None:
                game_start_time = game_start_time.replace(tzinfo=timezone.utc)
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
        game_start_time=game_start_time,
        condition_id=str(m.get("conditionId", "") or ""),
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
    """Generic paginated fetch from Gamma API with bounded retries."""
    results: list = []
    async with httpx.AsyncClient(timeout=GAMMA_TIMEOUT_SECONDS, trust_env=True) as client:
        for page in range(pages):
            params = {"limit": limit, "offset": page * limit, **(extra_params or {})}
            data = None
            for attempt in range(GAMMA_RETRY_ATTEMPTS):
                try:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as e:
                    body_preview = ""
                    try:
                        body_preview = (resp.text or "")[:160] if 'resp' in locals() else ""
                    except Exception:
                        body_preview = ""

                    if attempt < GAMMA_RETRY_ATTEMPTS - 1:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    if '503' in str(e) and ('Página bloqueada' in body_preview or '<html' in body_preview.lower()):
                        logger.warning(
                            "Gamma API bloqueada pela rede/origem (HTTP 503 + página de bloqueio). "
                            "Troque de rede (VPN/4G/outro IP) ou configure proxy HTTP(S)."
                        )
                    else:
                        logger.warning("Gamma API fetch page %d failed after retries: %s", page, e)
                    break
            if not data:
                break
            results.extend(data)
            await asyncio.sleep(0.15)
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


async def fetch_weather_markets(limit: int = 100) -> list[GammaMarket]:
    """Fetch daily-temperature markets by tag (volume ordering hides them)."""
    markets: list[GammaMarket] = []
    try:
        async with httpx.AsyncClient(timeout=GAMMA_TIMEOUT_SECONDS, trust_env=True) as client:
            resp = await client.get(
                f"{GAMMA_API}/events",
                params={"tag_id": WEATHER_TAG_ID, "active": "true", "closed": "false", "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
        if isinstance(data, list):
            for event in data:
                for raw in event.get("markets") or []:
                    m = _parse_market(raw)
                    if m and m.active and not m.closed:
                        markets.append(m)
    except Exception as e:
        logger.warning("Weather markets fetch failed: %s", e)
    logger.info("Fetched %d daily-temperature weather markets", len(markets))
    return markets

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
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
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
            # Direction requires momentum — flat near-50% has no directional alpha.
            if divergence < 0.15 and m.volume_24hr > VALUE_MIN_VOLUME * 2:
                if vol_liq_ratio > 1.5:
                    direction = "yes" if m.price_change_1d > 0.02 else "no"
                    # Scale edge by vol_liq_ratio strength (more volume over liq = more conviction).
                    edge = round(min(0.10, 0.04 + (vol_liq_ratio - 1.5) * 0.008), 4)
                    model_prob = m.yes_price + (edge if direction == "yes" else -edge)
                    signal_type = "high_volume_near_50"
                    confidence = 0.5 + min(vol_liq_ratio / 10, 0.3)

            # Pattern B: Low liquidity + high volume (informed money signal)
            # Direction requires momentum — without it, default NO as hedge.
            if m.liquidity < m.volume_24hr * 0.5 and m.volume_24hr > VALUE_MIN_VOLUME:
                direction = "yes" if m.price_change_1d > 0.02 else "no"
                model_prob = m.yes_price + (0.08 if direction == "yes" else -0.08)
                model_prob = max(0.01, min(0.99, model_prob))
                edge = abs(model_prob - m.yes_price)
                signal_type = "informed_money_low_liq"
                confidence = 0.4 + min(vol_liq_ratio / 5, 0.35)

            # Pattern C: Contrarian divergence — fade extreme prices. Direction is logically sound.
            if divergence >= VALUE_DIVERGENCE_THRESHOLD and m.volume_24hr > VALUE_MIN_VOLUME * 5:
                direction = "no" if m.yes_price > 0.65 else "yes"
                # Scale edge by divergence magnitude (further from 50% = stronger fade).
                edge = round(min(0.12, 0.04 + (divergence - 0.15) * 0.20), 4)
                model_prob = m.yes_price + (-edge if m.yes_price > 0.65 else edge)
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
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
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
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
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
    now = datetime.now(timezone.utc)

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
            # Filter out live/in-play sports markets: price_change_1d (24h momentum) is
            # stale once the game starts — the live game state moves faster than the signal.
            if m.game_start_time is not None and now >= m.game_start_time:
                continue
            if m.volume_24hr < 50000:
                continue
            if m.liquidity < SMART_MONEY_MIN_LIQUIDITY:
                continue

            avg_cat_vol = cat_avg.get(m.category, 50000)
            vol_ratio = m.volume_24hr / max(avg_cat_vol, 1)
            if vol_ratio < SMART_MONEY_VOLUME_RATIO:
                continue

            smart_score = (1 / max(m.spread, 0.001)) * vol_ratio

            # Direction from price change.
            # Require stronger evidence for YES (29% historical WR vs 45% for NO).
            # Flat markets default to NO: the uncertain zone (yes_price 0.5-0.6) had ~10% WR on YES.
            if m.price_change_1d > 0.03:
                direction = "yes"
            elif m.price_change_1d < -0.01:
                direction = "no"
            else:
                direction = "no"

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
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
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
# Strategy 5b: Smart Money Copy-Trading (shadow-only, additive)
#
# detect_smart_money (above) is a microstructure proxy (spread+volume+momentum).
# This is a *separate* strategy that follows real wallets via Polymarket's public
# data-api, gated behind a manually curated allowlist (PAPER_SMART_MONEY_WALLETS —
# the data-api has no leaderboard endpoint, so candidates must be picked from the
# public leaderboard webpage). It must stay in PAPER_SHADOW_STRATEGIES until its
# signals have been audited over real cycles — see feedback_strategy_pattern_caution
# on not swapping live strategy logic without validation.
# ---------------------------------------------------------------------------

async def _fetch_trader_positions(client: httpx.AsyncClient, wallet_address: str) -> List[Dict[str, Any]]:
    try:
        resp = await client.get(
            f"{DATA_API}/positions",
            params={"user": wallet_address, "sortBy": "CASHPNL", "sortDirection": "DESC", "limit": 200},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("Smart money copy: positions fetch failed for %s: %s", wallet_address, e)
        return []


async def _fetch_trader_closed_positions(client: httpx.AsyncClient, wallet_address: str) -> list[dict[str, Any]]:
    """Most recent resolved positions (realizedPnl per market) for quality scoring.

    /positions only returns OPEN positions (realizedPnl=0, no `active` field), so
    win-rate/profit-factor must come from /closed-positions, sorted by recency to
    avoid the PnL-sorted top-N inflating the win rate.
    """
    try:
        resp = await client.get(
            f"{DATA_API}/closed-positions",
            params={"user": wallet_address, "sortBy": "TIMESTAMP", "sortDirection": "DESC", "limit": 200},
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("Smart money copy: closed positions fetch failed for %s: %s", wallet_address, e)
        return []


async def _fetch_trader_trades(client: httpx.AsyncClient, wallet_address: str) -> List[Dict[str, Any]]:
    try:
        resp = await client.get(f"{DATA_API}/trades", params={"user": wallet_address, "limit": 100})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("Smart money copy: trades fetch failed for %s: %s", wallet_address, e)
        return []


def _trader_quality_score(closed_positions: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
    """Replicate MrFadiAi/Polymarket-bot's smart-money filters: WR >= 60%, profit
    factor >= 1.5, and no single closed position concentrating > 30% of total PnL.

    Expects /closed-positions entries (realizedPnl per resolved market).
    Returns a quality dict if the wallet passes all filters, else None.
    """
    pnls = [float(p.get("realizedPnl", p.get("cashPnl", 0.0)) or 0.0) for p in closed_positions]
    pnls = [p for p in pnls if p != 0.0]
    if len(pnls) < 5:
        return None

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    total_abs_pnl = sum(abs(p) for p in pnls)
    max_concentration = (max(abs(p) for p in pnls) / total_abs_pnl) if total_abs_pnl > 0 else 1.0

    if win_rate < SMART_MONEY_COPY_MIN_WIN_RATE:
        return None
    if profit_factor < SMART_MONEY_COPY_MIN_PROFIT_FACTOR:
        return None
    if max_concentration > SMART_MONEY_COPY_MAX_CONCENTRATION:
        return None

    return {
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_concentration": max_concentration,
        "sample_size": float(len(pnls)),
    }


async def detect_smart_money_copy(markets: List[GammaMarket]) -> List[Signal]:
    """Follow real top-trader wallets (copy-trading), shadow-only.

    For each wallet in PAPER_SMART_MONEY_WALLETS that passes _trader_quality_score,
    cross-reference its currently open positions against the scanned markets. When
    a tracked trader holds a side in a market we also see (and that market clears
    the same liquidity/spread bar as detect_smart_money), emit a signal following
    their side — sized by the relative weight of their position.

    Empty PAPER_SMART_MONEY_WALLETS => dormant strategy (no signals, no API calls).
    """
    signals: List[Signal] = []
    if not SMART_MONEY_COPY_WALLETS:
        return signals

    # Trader positions reference markets by conditionId (0x… hash), not by the
    # numeric Gamma id, so the cross-reference index must use condition_id.
    market_by_id = {m.condition_id: m for m in markets if m.condition_id}

    async with httpx.AsyncClient(timeout=SMART_MONEY_COPY_TIMEOUT_SECONDS, trust_env=True) as client:
        for wallet_address in SMART_MONEY_COPY_WALLETS:
            try:
                positions, closed_positions = await asyncio.gather(
                    _fetch_trader_positions(client, wallet_address),
                    _fetch_trader_closed_positions(client, wallet_address),
                )
                quality = _trader_quality_score(closed_positions, [])
                if not quality:
                    continue

                # /positions only returns open positions; redeemable=True means
                # already resolved (awaiting redemption), not copyable.
                open_positions = [p for p in positions if not bool(p.get("redeemable", False))]
                total_value = sum(abs(float(p.get("currentValue", p.get("initialValue", 0.0)) or 0.0)) for p in open_positions) or 1.0

                for p in open_positions:
                    condition_id = str(p.get("conditionId") or "")
                    m = market_by_id.get(condition_id)
                    if not m:
                        continue
                    if m.spread > SMART_MONEY_MAX_SPREAD or m.liquidity < SMART_MONEY_MIN_LIQUIDITY:
                        continue

                    outcome = str(p.get("outcome", "")).strip().lower()
                    direction = "yes" if outcome in {"yes", "up"} else "no"
                    entry_price = m.yes_price if direction == "yes" else m.no_price
                    if entry_price <= 0.03 or entry_price >= 0.97:
                        continue

                    position_value = abs(float(p.get("currentValue", p.get("initialValue", 0.0)) or 0.0))
                    weight = min(1.0, position_value / total_value)
                    edge = max(0.02, min(0.10, 0.03 + weight * 0.07))
                    model_prob = (
                        min(0.92, m.yes_price + edge) if direction == "yes"
                        else max(0.08, m.yes_price - edge)
                    )
                    confidence = min(0.85, 0.40 + quality["win_rate"] * 0.35 + weight * 0.15)
                    win_prob = model_prob if direction == "yes" else 1 - model_prob
                    kelly_frac, size = kelly_size(win_prob, entry_price, conservative=0.4, max_fraction=0.03)

                    signals.append(Signal(
                        strategy="smart_money_copy",
                        market_id=m.market_id,
                        event_slug=_slug_or_question(m),
                        event_title=_title_or_question(m),
                        direction=direction,
                        model_probability=model_prob,
                        market_probability=m.yes_price,
                        edge=edge,
                        confidence=confidence,
                        kelly_fraction=max(kelly_frac, 0.001),
                        suggested_size=size,
                        spread=m.spread,
                        liquidity=m.liquidity,
                        volume_24hr=m.volume_24hr,
                        reasoning=(
                            f"SMART_MONEY_COPY: wallet {wallet_address[:10]}… "
                            f"WR:{quality['win_rate']:.0%} PF:{quality['profit_factor']:.1f} "
                            f"side:{direction.upper()} weight:{weight:.0%} | {m.question[:50]}"
                        ),
                        sources=["gamma_api_markets", "polymarket_data_api", "smart_money_copy"],
                    ))
            except Exception as e:
                logger.debug("Smart money copy failed for wallet %s: %s", wallet_address, e)

    signals.sort(key=lambda s: s.confidence, reverse=True)
    logger.info("Smart Money Copy: %d signals found", len(signals))
    return signals


# ---------------------------------------------------------------------------
# Strategy 6: Event countdown (near resolution)
# ---------------------------------------------------------------------------

async def detect_event_countdown(markets: List[GammaMarket]) -> List[Signal]:
    """Detect directional opportunities in markets near end-date.

    Heuristic:
      - event ends soon (within configured hour window)
      - liquid/active enough to avoid dead books
      - avoid near-50% indecision and extreme tails
      - follow market-implied side with small confidence boost as expiry nears
    """
    signals: List[Signal] = []
    now = datetime.now(timezone.utc)

    for m in markets:
        try:
            if not m.end_date:
                continue
            if m.closed or not m.active:
                continue
            if m.liquidity < EVENT_COUNTDOWN_MIN_LIQUIDITY:
                continue
            if m.volume_24hr < EVENT_COUNTDOWN_MIN_VOL24H:
                continue
            if m.yes_price < 0.12 or m.yes_price > 0.88:
                continue

            hours_left = (m.end_date - now).total_seconds() / 3600.0
            if hours_left < EVENT_COUNTDOWN_MIN_HOURS or hours_left > EVENT_COUNTDOWN_MAX_HOURS:
                continue

            # Skip indecision zone near 50%.
            if abs(m.yes_price - 0.5) < EVENT_COUNTDOWN_MID_BAND:
                continue

            # Countdown sniper: as expiry nears, the market price IS the best estimate —
            # follow the favored side with a small confidence boost (not a momentum bet).
            direction = "yes" if m.yes_price > 0.5 else "no"
            market_anchor = m.yes_price if direction == "yes" else m.no_price

            # Boost increases as end approaches.
            urgency = 1.0 - (hours_left / max(EVENT_COUNTDOWN_MAX_HOURS, 0.1))
            urgency = max(0.0, min(1.0, urgency))
            boost = min(EVENT_COUNTDOWN_MAX_MODEL_BOOST, 0.03 + 0.05 * urgency)

            model_prob = min(0.95, market_anchor + boost)
            edge = model_prob - market_anchor
            if edge < ADVANCED_MIN_EDGE:
                continue

            confidence = min(0.86, 0.45 + 0.30 * urgency + 0.15 * min(m.volume_24hr / 200000.0, 1.0))

            # Convert back to YES-space probability expected by Signal schema.
            yes_model_prob = model_prob if direction == "yes" else (1.0 - model_prob)

            win_prob = model_prob
            entry_price = m.yes_price if direction == "yes" else m.no_price
            kelly_frac, size = kelly_size(win_prob, entry_price, conservative=0.6, max_fraction=0.035)
            kelly_frac = max(kelly_frac, 0.0015)

            signals.append(Signal(
                strategy="event_countdown",
                market_id=m.market_id,
                event_slug=_slug_or_question(m),
                event_title=_title_or_question(m),
                direction=direction,
                model_probability=yes_model_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                suggested_size=size,
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
                reasoning=(
                    f"COUNTDOWN: {m.question[:58]} | "
                    f"T-{hours_left:.1f}h MktYES:{m.yes_price:.0%} "
                    f"Dir:{direction.upper()} Edge:{edge:.1%}"
                ),
                sources=["gamma_api_markets", "event_countdown"],
            ))
        except Exception as e:
            logger.debug("Event countdown failed for market %s: %s", m.market_id, e)

    signals.sort(key=lambda s: s.confidence * s.edge, reverse=True)
    logger.info("Event Countdown: %d signals found", len(signals))
    return signals


# ---------------------------------------------------------------------------
# Strategy 7: Weather forecast (shadow-first)
# ---------------------------------------------------------------------------

async def _weather_geocode(client: httpx.AsyncClient, location: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(
            WEATHER_GEOCODE_API,
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return None
        r = results[0]
        if r.get("latitude") is None or r.get("longitude") is None:
            return None
        return {
            "name": r.get("name") or location,
            "admin1": r.get("admin1") or "",
            "country": r.get("country") or "",
            "latitude": float(r["latitude"]),
            "longitude": float(r["longitude"]),
        }
    except Exception as e:
        logger.debug("Weather geocode failed for %s: %s", location, e)
        return None


async def _weather_forecast(client: httpx.AsyncClient, geo: Dict[str, Any], unit: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(
            WEATHER_FORECAST_API,
            params={
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
                "temperature_unit": unit,
                "timezone": "auto",
                "forecast_days": WEATHER_MAX_DAYS_AHEAD,
            },
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("Weather forecast failed for %s: %s", geo.get("name"), e)
        return None


async def _weather_ensemble_forecast(client: httpx.AsyncClient, geo: Dict[str, Any], unit: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.get(
            WEATHER_ENSEMBLE_API,
            params={
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "hourly": "temperature_2m,precipitation",
                "temperature_unit": unit,
                "timezone": "auto",
                "forecast_days": WEATHER_MAX_DAYS_AHEAD,
                "models": "gfs_seamless",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("Weather ensemble forecast failed for %s: %s", geo.get("name"), e)
        return None


async def detect_weather_forecast(
    markets: List[GammaMarket],
    now: Optional[datetime] = None,
) -> List[Signal]:
    """Detect weather-market edges from Open-Meteo forecasts.

    This strategy is intended to run shadow-only initially. It only emits signals
    for markets whose location/date/metric can be parsed conservatively.
    """
    signals: List[Signal] = []
    if not WEATHER_ENABLED:
        return signals

    now = now or datetime.now(timezone.utc)
    geocode_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    forecast_cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}
    ensemble_cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}

    async with httpx.AsyncClient(timeout=WEATHER_TIMEOUT_SECONDS, trust_env=False) as client:
        for m in markets:
            try:
                if m.closed or not m.active:
                    continue
                if m.spread > WEATHER_MAX_SPREAD:
                    continue
                if m.liquidity < WEATHER_MIN_LIQUIDITY:
                    continue
                if m.yes_price < 0.03 or m.yes_price > 0.97:
                    continue
                if not _is_weather_market(m):
                    continue

                text = _weather_text(m)
                spec = _parse_weather_metric(text)
                if not spec:
                    continue
                date_iso = _parse_weather_date(text, now=now)
                location = _parse_weather_location(text)
                if not date_iso or not location:
                    continue

                target_dt = datetime.fromisoformat(date_iso).replace(tzinfo=timezone.utc)
                days_ahead = (target_dt.date() - now.date()).days
                if days_ahead < 0 or days_ahead >= WEATHER_MAX_DAYS_AHEAD:
                    continue

                geo = geocode_cache.get(location)
                if location not in geocode_cache:
                    geo = await _weather_geocode(client, location)
                    geocode_cache[location] = geo
                if not geo:
                    continue

                unit = str(spec.get("unit", "fahrenheit"))
                cache_key = (location, unit)

                # Prefer the 31-member GFS ensemble: probability comes from counting
                # how many members cross the market's threshold, not a heuristic curve.
                ensemble = ensemble_cache.get(cache_key)
                if cache_key not in ensemble_cache:
                    ensemble = await _weather_ensemble_forecast(client, geo, unit)
                    ensemble_cache[cache_key] = ensemble
                probability = _weather_probability_from_ensemble(spec, ensemble, date_iso) if ensemble else None
                forecast_source = "gfs_ensemble"

                if not probability:
                    forecast = forecast_cache.get(cache_key)
                    if cache_key not in forecast_cache:
                        forecast = await _weather_forecast(client, geo, unit)
                        forecast_cache[cache_key] = forecast
                    if not forecast:
                        continue
                    probability = _weather_probability_from_forecast(spec, forecast, date_iso)
                    forecast_source = "single_point_fallback"

                if not probability:
                    continue
                yes_prob, forecast_value, forecast_note = probability

                yes_edge = yes_prob - m.yes_price
                no_edge = (1.0 - yes_prob) - m.no_price
                if yes_edge >= WEATHER_MIN_EDGE:
                    direction = "yes"
                    edge = yes_edge
                    win_prob = yes_prob
                elif no_edge >= WEATHER_MIN_EDGE:
                    direction = "no"
                    edge = no_edge
                    win_prob = 1.0 - yes_prob
                else:
                    continue

                entry_price = m.yes_price if direction == "yes" else m.no_price
                kelly_frac, size = kelly_size(win_prob, entry_price, conservative=0.35, max_fraction=0.025)
                confidence = min(0.82, 0.42 + edge * 2.2 + min(m.liquidity / 50000.0, 0.12))

                location_label = ", ".join(x for x in [geo.get("name"), geo.get("admin1"), geo.get("country")] if x)
                metric_label = spec["metric"]
                signals.append(Signal(
                    strategy="weather_forecast",
                    market_id=m.market_id,
                    event_slug=_slug_or_question(m),
                    event_title=_title_or_question(m),
                    direction=direction,
                    model_probability=yes_prob,
                    market_probability=m.yes_price,
                    edge=edge,
                    confidence=confidence,
                    kelly_fraction=max(kelly_frac, 0.001),
                    suggested_size=min(size, ADVANCED_MAX_TRADE_SIZE),
                    spread=m.spread,
                    liquidity=m.liquidity,
                    volume_24hr=m.volume_24hr,
                    reasoning=(
                        f"WEATHER_FORECAST[{forecast_source}]: {metric_label} {location_label} {date_iso} | "
                        f"{forecast_note} modelYES:{yes_prob:.0%} mktYES:{m.yes_price:.0%} "
                        f"dir:{direction.upper()} edge:{edge:.1%}"
                    ),
                    sources=["gamma_api_markets", "open_meteo", forecast_source],
                ))
            except Exception as e:
                logger.debug("Weather forecast detection failed for market %s: %s", m.market_id, e)

    signals.sort(key=lambda s: s.confidence * abs(s.edge), reverse=True)
    logger.info("Weather Forecast: %d signals found", len(signals))
    return signals


# ---------------------------------------------------------------------------
# Strategy 8: BTC 5-minute momentum/scalping
# ---------------------------------------------------------------------------

def _btc_5m_start_ts_from_slug(slug: str) -> Optional[int]:
    try:
        if not slug.startswith("btc-updown-5m-"):
            return None
        return int(slug.rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None

async def fetch_btc_5m_events(window_before: int = 1, window_after: int = 2) -> List[Dict[str, Any]]:
    """Fetch current and adjacent BTC Up/Down 5m events by deterministic slugs."""
    if not BTC_5M_ENABLED:
        return []
    now_ts = int(datetime.now(timezone.utc).timestamp())
    current_start = now_ts - (now_ts % 300)
    slugs = [f"btc-updown-5m-{current_start + i * 300}" for i in range(-window_before, window_after + 1)]
    async with httpx.AsyncClient(timeout=GAMMA_TIMEOUT_SECONDS, trust_env=True) as client:
        async def fetch_slug(slug: str) -> Optional[Dict[str, Any]]:
            for attempt in range(GAMMA_RETRY_ATTEMPTS):
                try:
                    resp = await client.get(f"{GAMMA_API}/events", params={"slug": slug})
                    resp.raise_for_status()
                    data = resp.json()
                    if isinstance(data, list) and data:
                        return data[0]
                    return None
                except Exception as e:
                    if attempt < GAMMA_RETRY_ATTEMPTS - 1:
                        await asyncio.sleep(0.3 * (attempt + 1))
                        continue
                    logger.warning("BTC 5m fetch failed for %s after retries: %s", slug, e)
            return None

        results = await asyncio.gather(*(fetch_slug(slug) for slug in slugs))
    return [event for event in results if event]

async def _fetch_btc_klines(client: httpx.AsyncClient, interval: str = "1m", limit: int = 20) -> Optional[List[float]]:
    """Fetch recent BTC/USDT close prices from Binance's public klines endpoint.

    Real spot price-action — independent of Polymarket's own implied odds — used
    purely as a confirming signal for btc_5m_momentum (never to set direction).
    """
    try:
        resp = await client.get(
            BTC_KLINES_API,
            params={"symbol": "BTCUSDT", "interval": interval, "limit": limit},
            timeout=BTC_KLINES_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return None
        return [float(candle[4]) for candle in data]
    except Exception as e:
        logger.debug("BTC klines fetch failed: %s", e)
        return None


def _parse_clob_token_ids(raw_market: dict[str, Any]) -> list[str]:
    """Extract CLOB token ids from a raw Gamma market (JSON-encoded string or list)."""
    tokens = raw_market.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            return []
    if not isinstance(tokens, list):
        return []
    return [str(t) for t in tokens if t]


async def _fetch_clob_yes_midpoint(client: httpx.AsyncClient, raw_market: dict[str, Any]) -> float | None:
    """Live YES midpoint from the CLOB orderbook; None on any failure."""
    tokens = _parse_clob_token_ids(raw_market)
    if not tokens:
        return None
    try:
        resp = await client.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": tokens[0]},
            timeout=CLOB_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        mid = float(resp.json().get("mid"))
    except Exception as e:
        logger.debug("CLOB midpoint fetch failed: %s", e)
        return None
    if not (0.0 < mid < 1.0):
        return None
    return mid


def _with_live_prices(m: GammaMarket, mid: float | None) -> GammaMarket:
    """Return a copy of the market repriced at the live CLOB midpoint (no-op if mid is None)."""
    if mid is None:
        return m
    return replace(m, yes_price=round(mid, 6), no_price=round(1.0 - mid, 6))


def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Classic Wilder RSI over the given close series."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(0.0, delta))
        losses.append(max(0.0, -delta))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_momentum(closes: List[float], lookback: int) -> Optional[float]:
    """Percent change between the latest close and the close `lookback` candles back."""
    if len(closes) <= lookback:
        return None
    base = closes[-1 - lookback]
    if base == 0:
        return None
    return (closes[-1] - base) / base


async def detect_btc_5m_momentum(events: Optional[List[Dict[str, Any]]] = None) -> List[Signal]:
    """Detect BTC 5m Up/Down momentum signals.

    Observable @ndjjwobaq-like pattern implemented for paper trading:
    - only BTC Up/Down 5-minute markets;
    - enter after the interval begins, not before;
    - follow the side whose Polymarket live probability has moved away from 50/50;
    - prefer <=0.72 entry prices, with conservative late-confirmation up to configured cap;
    - BUY-only paper entry and hold/settle via the existing deterministic settlement.
    """
    signals: List[Signal] = []
    if not BTC_5M_ENABLED:
        return signals
    if events is None:
        events = await fetch_btc_5m_events()

    # Real BTC/USDT price action — fetched once per cycle (shared across all
    # events, since they all track the same underlying). This is purely a
    # confirmation signal: it never decides `direction`, only nudges
    # confidence/sizing when it agrees or disagrees with the market consensus
    # (see feedback_strategy_pattern_caution: btc_5m_momentum follows consensus).
    rsi_14: Optional[float] = None
    momentum_5m: Optional[float] = None
    async with httpx.AsyncClient(timeout=BTC_KLINES_TIMEOUT_SECONDS, trust_env=True) as btc_client:
        closes = await _fetch_btc_klines(btc_client, interval="1m", limit=20)
    if closes:
        rsi_14 = _compute_rsi(closes, period=14)
        momentum_5m = _compute_momentum(closes, lookback=5)

    price_action_side: Optional[str] = None
    if rsi_14 is not None and momentum_5m is not None:
        if rsi_14 > 55 and momentum_5m > 0:
            price_action_side = "yes"
        elif rsi_14 < 45 and momentum_5m < 0:
            price_action_side = "no"

    now_ts = int(datetime.now(timezone.utc).timestamp())
    for event in events:
        try:
            slug = str(event.get("slug") or "")
            if not slug.startswith("btc-updown-5m-"):
                continue
            start_ts = _btc_5m_start_ts_from_slug(slug)
            if start_ts is None:
                continue
            seconds_in = now_ts - start_ts
            if seconds_in < BTC_5M_MIN_SECONDS_IN or seconds_in > BTC_5M_MAX_SECONDS_IN:
                continue
            if bool(event.get("closed", False)) or not bool(event.get("active", True)):
                continue
            markets = event.get("markets") or []
            if not markets:
                continue
            raw_market = markets[0]
            m = _parse_market(raw_market)
            if not m or m.closed or not m.active:
                continue
            if m.liquidity < BTC_5M_MIN_LIQUIDITY:
                continue
            # Reprice off the live CLOB midpoint: Gamma prices lag these 5m
            # markets by enough to invalidate every gate below.
            async with httpx.AsyncClient(trust_env=True) as clob_client:
                m = _with_live_prices(m, await _fetch_clob_yes_midpoint(clob_client, raw_market))
            directional_edge = abs(m.yes_price - 0.5)
            if directional_edge < BTC_5M_MIN_DIRECTIONAL_EDGE:
                continue

            direction = "yes" if m.yes_price > m.no_price else "no"
            entry_price = m.yes_price if direction == "yes" else m.no_price
            if entry_price <= 0 or entry_price >= 1:
                continue
            normal_entry = entry_price <= BTC_5M_NORMAL_MAX_PRICE
            late_confirm = seconds_in >= BTC_5M_LATE_CONFIRM_MIN_SECONDS and entry_price <= BTC_5M_LATE_CONFIRM_MAX_PRICE
            if not normal_entry and not late_confirm:
                continue

            # Model probability is intentionally modest: odds are the signal, but
            # this is a very short-horizon binary trade with high variance.
            time_score = max(0.0, min(1.0, seconds_in / 300.0))
            raw_edge = 0.06 + directional_edge * 0.55 + max(0.0, time_score - 0.55) * 0.08
            edge = max(0.0, raw_edge - _high_entry_price_penalty(entry_price))
            model_win_prob = min(0.95, entry_price + edge)
            confidence = min(0.88, 0.48 + directional_edge * 1.8 + min(m.volume_24hr / 5000.0, 0.12) + time_score * 0.08)
            if entry_price > BTC_5M_HIGH_PRICE_PENALTY_START:
                confidence *= 0.85
            # More aggressive than the old generic strategies but still capped by wallet max_trade.
            size_multiplier = 0.70 + min(directional_edge / 0.20, 1.0) * 0.60
            if entry_price > BTC_5M_HIGH_PRICE_PENALTY_START:
                size_multiplier *= 0.60

            # Real-price confirmation: nudge confidence/edge/size, never the direction.
            price_action_note = "neutral"
            if price_action_side is not None:
                if price_action_side == direction:
                    edge = min(0.30, edge + 0.01)
                    confidence = min(0.92, confidence * 1.10)
                    size_multiplier *= 1.10
                    price_action_note = "confirms"
                else:
                    confidence *= 0.85
                    size_multiplier *= 0.85
                    price_action_note = "diverges"

            suggested_size = min(ADVANCED_MAX_TRADE_SIZE, BTC_5M_BASE_SIZE * size_multiplier)
            kelly_frac = min(0.05, suggested_size / max(INITIAL_BANKROLL, 1.0))
            side_label = "UP" if direction == "yes" else "DOWN"
            entry_mode = "late_confirm" if late_confirm and not normal_entry else "momentum"

            signals.append(Signal(
                strategy="btc_5m_momentum",
                market_id=m.market_id,
                event_slug=slug,
                event_title=str(event.get("title") or m.question),
                direction=direction,
                model_probability=model_win_prob if direction == "yes" else 1.0 - model_win_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                suggested_size=suggested_size,
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
                reasoning=(
                    f"BTC_5M_{entry_mode}: BUY {side_label} aos {seconds_in}s/300s | "
                    f"Up:{m.yes_price:.1%} Down:{m.no_price:.1%} entry:{entry_price:.1%} "
                    f"edge:{edge:.1%} liq:${m.liquidity:,.0f} vol24h:${m.volume_24hr:,.0f} | "
                    f"price_action:{price_action_note}"
                    + (f" rsi14:{rsi_14:.0f} mom5m:{momentum_5m:+.2%}" if rsi_14 is not None and momentum_5m is not None else "")
                ),
                sources=["gamma_api_btc_5m", "ndjjwobaq_profile_inspired", "binance_klines"],
            ))
        except Exception as e:
            logger.debug("BTC 5m momentum failed for event: %s", e)

    signals.sort(key=lambda s: s.confidence * abs(s.edge), reverse=True)
    logger.info("BTC 5m Momentum: %d signals found", len(signals))
    return signals


async def detect_endgame_last_minute(events: Optional[List[Dict[str, Any]]] = None) -> List[Signal]:
    """Dedicated endgame strategy for BTC 5m markets in the final minute.

    Trades only inside [ENDGAME_WINDOW_START_SECONDS, ENDGAME_WINDOW_END_SECONDS]
    using stronger directional imbalance and tighter liquidity constraints.
    """
    signals: List[Signal] = []
    if not ENDGAME_ENABLED:
        return signals
    if events is None:
        events = await fetch_btc_5m_events()

    now_ts = int(datetime.now(timezone.utc).timestamp())
    for event in events:
        try:
            slug = str(event.get("slug") or "")
            if not slug.startswith("btc-updown-5m-"):
                continue
            start_ts = _btc_5m_start_ts_from_slug(slug)
            if start_ts is None:
                continue

            seconds_in = now_ts - start_ts
            if seconds_in < ENDGAME_WINDOW_START_SECONDS or seconds_in > ENDGAME_WINDOW_END_SECONDS:
                continue
            if bool(event.get("closed", False)) or not bool(event.get("active", True)):
                continue

            markets = event.get("markets") or []
            if not markets:
                continue
            m = _parse_market(markets[0])
            if not m or m.closed or not m.active:
                continue
            if m.liquidity < ENDGAME_MIN_LIQUIDITY:
                continue

            # Last-minute entries are hypersensitive to price staleness: reprice
            # off the live CLOB midpoint before any directional decision.
            async with httpx.AsyncClient(trust_env=True) as clob_client:
                m = _with_live_prices(m, await _fetch_clob_yes_midpoint(clob_client, markets[0]))
            directional_edge = abs(m.yes_price - 0.5)
            if directional_edge < ENDGAME_MIN_DIRECTIONAL_EDGE:
                continue

            # Last-minute sniper: follow the side the market already favors (price reflects near-confirmed outcome).
            direction = "yes" if m.yes_price > m.no_price else "no"
            entry_price = m.yes_price if direction == "yes" else m.no_price
            if entry_price <= 0 or entry_price >= ENDGAME_MAX_ENTRY_PRICE:
                continue

            time_left = max(1, 300 - seconds_in)
            urgency = 1.0 - (time_left / 60.0)  # 0..1 over final minute
            urgency = max(0.0, min(1.0, urgency))
            raw_edge = 0.05 + directional_edge * 0.70 + urgency * 0.06
            edge = max(0.0, raw_edge - _high_entry_price_penalty(entry_price))
            model_win_prob = min(0.965, entry_price + edge)
            if edge < ADVANCED_MIN_EDGE:
                continue

            confidence = min(0.93, 0.56 + directional_edge * 1.6 + urgency * 0.18)
            size_mult = 0.9 + min(directional_edge / 0.18, 1.0) * 0.5
            if entry_price > BTC_5M_HIGH_PRICE_PENALTY_START:
                confidence *= 0.85
                size_mult *= 0.60
            suggested_size = min(ADVANCED_MAX_TRADE_SIZE, ENDGAME_BASE_SIZE * size_mult)
            kelly_frac = min(0.06, suggested_size / max(INITIAL_BANKROLL, 1.0))

            signals.append(Signal(
                strategy="endgame_last_minute",
                market_id=m.market_id,
                event_slug=slug,
                event_title=str(event.get("title") or m.question),
                direction=direction,
                model_probability=model_win_prob if direction == "yes" else 1.0 - model_win_prob,
                market_probability=m.yes_price,
                edge=edge,
                confidence=confidence,
                kelly_fraction=kelly_frac,
                suggested_size=suggested_size,
                spread=m.spread,
                liquidity=m.liquidity,
                volume_24hr=m.volume_24hr,
                reasoning=(
                    f"ENDGAME_LAST_MINUTE: {seconds_in}s/300s | left:{time_left}s "
                    f"dir:{direction.upper()} up:{m.yes_price:.1%} down:{m.no_price:.1%} "
                    f"edge:{edge:.1%} liq:${m.liquidity:,.0f}"
                ),
                sources=["gamma_api_btc_5m", "endgame_last_minute"],
            ))
        except Exception as e:
            logger.debug("Endgame last-minute failed for event: %s", e)

    signals.sort(key=lambda s: s.confidence * abs(s.edge), reverse=True)
    logger.info("Endgame Last-Minute: %d signals found", len(signals))
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

    async def _fetch_or_empty(name: str, fetcher):
        try:
            return await fetcher()
        except Exception as e:
            logger.error("Failed to fetch %s: %s", name, e)
            return []

    markets, events, btc_5m_events, weather_markets = await asyncio.gather(
        _fetch_or_empty("Gamma markets", fetch_gamma_markets),
        _fetch_or_empty("Gamma events", fetch_gamma_events),
        _fetch_or_empty("BTC 5m events", fetch_btc_5m_events),
        _fetch_or_empty("Weather markets", fetch_weather_markets),
    )

    total_markets = len(markets)
    logger.info("Fetched %d markets, %d events", total_markets, len(events))

    # Weather-only universe: the general list plus tag-fetched temperature
    # markets (deduped). Other strategies keep the volume-sorted universe.
    general_ids = {m.market_id for m in markets}
    weather_universe = markets + [m for m in weather_markets if m.market_id not in general_ids]

    # Run strategies (each wrapped in try/except)
    all_signals: List[Signal] = []
    strategy_funcs: List[Tuple[str, Any]] = [
        ("BTC 5m Momentum", lambda: detect_btc_5m_momentum(btc_5m_events)),
        ("Endgame Last-Minute", lambda: detect_endgame_last_minute(btc_5m_events)),
        ("Arbitrage", lambda: detect_arbitrage(events)),
        ("Value", lambda: detect_value_betting(markets)),
        ("Mean Reversion", lambda: detect_mean_reversion(markets)),
        ("Volume Spike", lambda: detect_volume_spikes(markets)),
        ("Smart Money", lambda: detect_smart_money(markets)),
        ("Smart Money Copy", lambda: detect_smart_money_copy(markets)),
        ("Event Countdown", lambda: detect_event_countdown(markets)),
        ("Weather Forecast", lambda: detect_weather_forecast(weather_universe)),
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

async def scan_btc_5m_only() -> Dict[str, Any]:
    """Fast scan for only the BTC 5-minute momentum strategy."""
    logger.info("=" * 50)
    logger.info("BTC 5M MOMENTUM SCAN: Fetching current BTC Up/Down markets...")
    try:
        btc_5m_events = await fetch_btc_5m_events()
    except Exception as e:
        logger.error("Failed to fetch BTC 5m events: %s", e)
        btc_5m_events = []
    signals = await detect_btc_5m_momentum(btc_5m_events)
    actionable = [s for s in signals if s.passes_threshold]
    logger.info("BTC 5M SCAN COMPLETE: %d total signals, %d actionable", len(signals), len(actionable))
    logger.info("=" * 50)
    return {
        "total_markets": len(btc_5m_events),
        "total_events": len(btc_5m_events),
        "total_signals": len(signals),
        "actionable_signals": len(actionable),
        "signals": [s.to_dict() for s in signals],
        "by_strategy": {"btc_5m_momentum": len(signals)} if signals else {},
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
