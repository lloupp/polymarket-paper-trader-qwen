"""Shared utilities used across scanner, settlement, learning and ops modules."""
from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Strategy catalogue
# ---------------------------------------------------------------------------
KNOWN_STRATEGIES: list[str] = [
    "btc_5m_momentum",
    "endgame_last_minute",
    "arbitrage",
    "value",
    "mean_reversion",
    "volume_spike",
    "smart_money",
    "event_countdown",
    "weather_forecast",
]
RECOMMENDED_MODE = "btc_5m_momentum,endgame_last_minute,smart_money,event_countdown,weather_forecast"

# ---------------------------------------------------------------------------
# Polymarket DNS fallback
# WSL/local DNS can intermittently fail resolving Polymarket Cloudflare hosts.
# Keep URL hostnames intact (TLS SNI still works) but bypass resolver failures
# with the current Cloudflare A records unless explicitly disabled.
# ---------------------------------------------------------------------------
POLYMARKET_STATIC_DNS = os.getenv("PAPER_POLYMARKET_STATIC_DNS", "0") == "1"
POLYMARKET_STATIC_IP = os.getenv("PAPER_POLYMARKET_STATIC_IP", "104.18.34.205")
_POLYMARKET_DNS_HOSTS = {"gamma-api.polymarket.com", "data-api.polymarket.com", "clob.polymarket.com"}
_ORIG_GETADDRINFO = socket.getaddrinfo


def install_polymarket_dns_fallback() -> None:
    if not POLYMARKET_STATIC_DNS or getattr(socket.getaddrinfo, "_polymarket_fallback", False):
        return

    def _patched(host, port, family=0, type=0, proto=0, flags=0):
        decoded = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if decoded in _POLYMARKET_DNS_HOSTS:
            host = POLYMARKET_STATIC_IP
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)

    _patched._polymarket_fallback = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _patched


install_polymarket_dns_fallback()

# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_dt(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string to a timezone-aware UTC datetime, or return None."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# JSON I/O helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any, *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Polymarket market price helpers
# ---------------------------------------------------------------------------

def yes_no_from_gamma_market(data: dict[str, Any]) -> tuple[float, float]:
    """Extract (yes_price, no_price) from a Gamma API market dict."""
    yes_price, no_price = 0.5, 0.5
    prices = data.get("outcomePrices", "")
    if prices:
        try:
            parsed = json.loads(prices) if isinstance(prices, str) else prices
            if isinstance(parsed, list) and len(parsed) >= 2:
                yes_price, no_price = float(parsed[0]), float(parsed[1])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    best_bid = float(data.get("bestBid", 0) or 0)
    best_ask = float(data.get("bestAsk", 0) or 0)
    if 0 < best_bid <= 1 and 0 < best_ask <= 1 and best_bid <= best_ask:
        yes_price = (best_bid + best_ask) / 2.0
        no_price = 1.0 - yes_price

    return yes_price, no_price
