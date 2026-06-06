import sys
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


def test_best_book_price_uses_max_bid_and_min_ask_even_if_unsorted():
    levels = [{"price": "0.01"}, {"price": "0.52"}, {"price": "0.35"}]

    assert _best_book_price(levels, best="bid") == 0.52
    assert _best_book_price(levels, best="ask") == 0.01
