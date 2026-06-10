import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scanner import (
    GammaMarket,
    _compute_momentum,
    _compute_rsi,
    _parse_clob_token_ids,
    _trader_quality_score,
    _weather_probability_from_ensemble,
    _with_live_prices,
    detect_smart_money,
)
from settlement import _streak_size_multiplier
from wallet import DEFAULT_SETTINGS


class RsiMomentumTest(unittest.TestCase):
    def test_rsi_all_gains_is_100(self):
        closes = [float(i) for i in range(1, 17)]  # strictly increasing
        self.assertEqual(_compute_rsi(closes, period=14), 100.0)

    def test_rsi_all_losses_is_zero(self):
        closes = [float(i) for i in range(16, 0, -1)]  # strictly decreasing
        self.assertEqual(_compute_rsi(closes, period=14), 0.0)

    def test_rsi_needs_enough_candles(self):
        self.assertIsNone(_compute_rsi([1.0, 2.0, 3.0], period=14))

    def test_momentum_percent_change(self):
        closes = [100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
        self.assertAlmostEqual(_compute_momentum(closes, lookback=5), 0.10)

    def test_momentum_needs_enough_candles(self):
        self.assertIsNone(_compute_momentum([1.0, 2.0], lookback=5))


class WeatherEnsembleProbabilityTest(unittest.TestCase):
    def test_temperature_probability_counts_members_above_threshold(self):
        spec = {"metric": "temperature", "operator": "above", "threshold": 90.0, "unit": "fahrenheit", "temp_kind": "max"}
        ensemble = {
            "hourly": {
                "time": ["2026-06-10T00:00", "2026-06-10T12:00", "2026-06-11T00:00"],
                "temperature_2m_member01": [80.0, 95.0, 70.0],
                "temperature_2m_member02": [80.0, 88.0, 70.0],
                "temperature_2m_member03": [80.0, 91.0, 70.0],
            }
        }
        result = _weather_probability_from_ensemble(spec, ensemble, "2026-06-10")
        self.assertIsNotNone(result)
        yes_prob, median_value, note = result
        # Members 01 and 03 cross 90F on the target day; member 02 does not -> 2/3
        self.assertAlmostEqual(yes_prob, 2.0 / 3.0)
        self.assertIn("ensemble_max", note)

    def test_rain_probability_counts_members_with_measurable_rain(self):
        spec = {"metric": "rain"}
        ensemble = {
            "hourly": {
                "time": ["2026-06-10T00:00", "2026-06-10T01:00"],
                "precipitation_member01": [0.4, 0.3],
                "precipitation_member02": [0.0, 0.0],
            }
        }
        result = _weather_probability_from_ensemble(spec, ensemble, "2026-06-10")
        self.assertIsNotNone(result)
        yes_prob, _, note = result
        self.assertAlmostEqual(yes_prob, 0.5)
        self.assertIn("ensemble_rain", note)

    def test_returns_none_without_matching_date(self):
        spec = {"metric": "temperature", "operator": "above", "threshold": 90.0, "unit": "fahrenheit", "temp_kind": "max"}
        ensemble = {"hourly": {"time": ["2026-06-11T00:00"], "temperature_2m_member01": [95.0]}}
        self.assertIsNone(_weather_probability_from_ensemble(spec, ensemble, "2026-06-10"))


class StreakSizeMultiplierTest(unittest.TestCase):
    def test_no_history_returns_neutral_multiplier(self):
        self.assertEqual(_streak_size_multiplier([], DEFAULT_SETTINGS), 1.0)

    def test_consecutive_losses_shrink_size(self):
        history = [
            {"pnl": 1.0, "trusted_for_pnl": True},
            {"pnl": -2.0, "trusted_for_pnl": True},
            {"pnl": -3.0, "trusted_for_pnl": True},
        ]
        mult = _streak_size_multiplier(history, DEFAULT_SETTINGS)
        self.assertAlmostEqual(mult, 1.0 - DEFAULT_SETTINGS["streak_loss_decay"] * 2)

    def test_consecutive_wins_grow_size_capped(self):
        history = [{"pnl": 1.0, "trusted_for_pnl": True} for _ in range(10)]
        mult = _streak_size_multiplier(history, DEFAULT_SETTINGS)
        self.assertEqual(mult, DEFAULT_SETTINGS["streak_size_max_mult"])

    def test_loss_streak_floors_at_min_multiplier(self):
        history = [{"pnl": -1.0, "trusted_for_pnl": True} for _ in range(10)]
        mult = _streak_size_multiplier(history, DEFAULT_SETTINGS)
        self.assertEqual(mult, DEFAULT_SETTINGS["streak_size_min_mult"])

    def test_untrusted_history_is_ignored(self):
        history = [{"pnl": -5.0, "trusted_for_pnl": False}, {"pnl": 1.0, "trusted_for_pnl": True}]
        mult = _streak_size_multiplier(history, DEFAULT_SETTINGS)
        self.assertEqual(mult, 1.0 + DEFAULT_SETTINGS["streak_win_boost"] * 1)


class TraderQualityScoreTest(unittest.TestCase):
    def test_qualifies_trader_with_strong_track_record(self):
        positions = [
            {"active": False, "redeemable": True, "cashPnl": pnl}
            for pnl in [50, 40, 30, 20, -10, 60, -5]
        ]
        quality = _trader_quality_score(positions, [])
        self.assertIsNotNone(quality)
        self.assertGreaterEqual(quality["win_rate"], 0.6)
        self.assertGreaterEqual(quality["profit_factor"], 1.5)

    def test_rejects_low_win_rate(self):
        positions = [
            {"active": False, "redeemable": True, "cashPnl": pnl}
            for pnl in [10, -10, -10, -10, 10]
        ]
        self.assertIsNone(_trader_quality_score(positions, []))

    def test_rejects_concentrated_pnl(self):
        positions = [
            {"active": False, "redeemable": True, "cashPnl": pnl}
            for pnl in [500, 5, 5, 5, 5, 5]
        ]
        self.assertIsNone(_trader_quality_score(positions, []))

    def test_rejects_too_few_samples(self):
        positions = [{"active": False, "redeemable": True, "cashPnl": 10}]
        self.assertIsNone(_trader_quality_score(positions, []))


def _smart_money_market(market_id, game_start_time, *, price_change_1d=0.05):
    return GammaMarket(
        market_id=market_id,
        event_slug=f"market-{market_id}",
        question="Will the home team win?",
        yes_price=0.5,
        no_price=0.5,
        spread=0.01,
        volume_24hr=200000,
        liquidity=50000,
        price_change_1d=price_change_1d,
        game_start_time=game_start_time,
    )


class SmartMoneyLiveGameFilterTest(unittest.IsolatedAsyncioTestCase):
    async def test_excludes_market_whose_game_already_started(self):
        now = datetime.now(timezone.utc)
        live = _smart_money_market("live", now - timedelta(minutes=30))
        signals = await detect_smart_money([live])
        self.assertFalse(any(s.market_id == "live" for s in signals))

    async def test_keeps_pregame_sports_market(self):
        now = datetime.now(timezone.utc)
        pregame = _smart_money_market("pregame", now + timedelta(hours=2))
        signals = await detect_smart_money([pregame])
        self.assertTrue(any(s.market_id == "pregame" for s in signals))

    async def test_keeps_non_sports_market_without_game_start_time(self):
        non_sports = _smart_money_market("nonsports", None)
        signals = await detect_smart_money([non_sports])
        self.assertTrue(any(s.market_id == "nonsports" for s in signals))


class ClobLivePriceTest(unittest.TestCase):
    def test_parses_json_encoded_token_ids(self):
        raw = {"clobTokenIds": '["111", "222"]'}
        self.assertEqual(_parse_clob_token_ids(raw), ["111", "222"])

    def test_parses_list_token_ids(self):
        raw = {"clobTokenIds": [111, 222]}
        self.assertEqual(_parse_clob_token_ids(raw), ["111", "222"])

    def test_returns_empty_on_missing_or_malformed(self):
        self.assertEqual(_parse_clob_token_ids({}), [])
        self.assertEqual(_parse_clob_token_ids({"clobTokenIds": "not-json"}), [])
        self.assertEqual(_parse_clob_token_ids({"clobTokenIds": 42}), [])

    def test_with_live_prices_reprices_both_sides(self):
        m = _smart_money_market("m1", None)
        live = _with_live_prices(m, 0.85)
        self.assertAlmostEqual(live.yes_price, 0.85)
        self.assertAlmostEqual(live.no_price, 0.15)
        # original untouched (dataclasses.replace returns a copy)
        self.assertAlmostEqual(m.yes_price, 0.5)

    def test_with_live_prices_none_is_noop(self):
        m = _smart_money_market("m1", None)
        self.assertIs(_with_live_prices(m, None), m)


if __name__ == "__main__":
    unittest.main()
