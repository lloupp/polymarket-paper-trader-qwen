import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlement import _best_book_price, get_entry_quote, resolved_side_prices_from_gamma


def test_entry_quote_uses_buy_price_for_selected_side():
    market_data = {
        "yes_buy_price": 0.53,
        "yes_sell_price": 0.49,
        "no_buy_price": 0.52,
        "no_sell_price": 0.47,
    }

    assert get_entry_quote("YES", market_data) == 0.53
    assert get_entry_quote("NO", market_data) == 0.52


def test_resolved_side_prices_accepts_binary_outcomes_only():
    assert resolved_side_prices_from_gamma({"outcomePrices": '["1", "0"]'}) == (1.0, 0.0)
    assert resolved_side_prices_from_gamma({"outcomePrices": [0, 1]}) == (0.0, 1.0)
    assert resolved_side_prices_from_gamma({"outcomePrices": '["0.52", "0.48"]'}) is None


def test_resolved_side_prices_accepts_5050_void_after_grace_period():
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    # Voided market (e.g. tennis walkover): closed long ago, stuck at 0.5/0.5.
    assert resolved_side_prices_from_gamma(
        {"outcomePrices": '["0.5", "0.5"]', "closed": True, "closedTime": old}
    ) == (0.5, 0.5)
    # Gamma's closedTime format with "+00" offset shorthand.
    assert resolved_side_prices_from_gamma(
        {"outcomePrices": '["0.5", "0.5"]', "closed": True, "closedTime": "2026-05-28 17:59:38+00"}
    ) == (0.5, 0.5)
    # Falls back to endDate when closedTime is missing.
    assert resolved_side_prices_from_gamma(
        {"outcomePrices": '["0.5", "0.5"]', "closed": True, "endDate": old}
    ) == (0.5, 0.5)

    # Recently closed 0.5/0.5 may still be a halted snapshot — not trusted.
    assert resolved_side_prices_from_gamma(
        {"outcomePrices": '["0.5", "0.5"]', "closed": True, "closedTime": recent}
    ) is None
    # Not closed, or no timestamp at all: never trusted.
    assert resolved_side_prices_from_gamma(
        {"outcomePrices": '["0.5", "0.5"]', "closed": False, "closedTime": old}
    ) is None
    assert resolved_side_prices_from_gamma({"outcomePrices": '["0.5", "0.5"]', "closed": True}) is None


def test_best_book_price_uses_max_bid_and_min_ask_even_if_unsorted():
    levels = [{"price": "0.01"}, {"price": "0.52"}, {"price": "0.35"}]

    assert _best_book_price(levels, best="bid") == 0.52
    assert _best_book_price(levels, best="ask") == 0.01
