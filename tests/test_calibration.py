import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from learning import build_probability_tables, calibrated_side_probability


def _outcome(strategy, model_prob, direction, won):
    return {
        "event_type": "signal_outcome",
        "observed_side_price": 0.99 if won else 0.01,
        "original_signal": {
            "strategy": strategy,
            "model_probability": model_prob,
            "direction": direction,
        },
    }


def _write_events(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


class ProbabilityTablesTest(unittest.TestCase):
    def test_builds_realized_rate_per_strategy_decile(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            # decil d7 (p_side 0.70-0.79): modelo superconfiante, só 25% ganha
            events = [_outcome("weather_forecast", 0.75, "yes", won=(i % 4 == 0)) for i in range(40)]
            # direção NO: p_side = 1 - 0.25 = 0.75 → mesmo decil
            events += [_outcome("weather_forecast", 0.25, "no", won=False) for _ in range(8)]
            _write_events(log, events)

            tables = build_probability_tables(log, min_bucket_n=30)
            bucket = tables["weather_forecast"]["d7"]
            self.assertEqual(bucket["n"], 48)
            self.assertTrue(bucket["reliable"])
            # 10 vitórias em 48, com Laplace: (10+1)/(48+2)
            self.assertAlmostEqual(bucket["realized"], (10 + 1) / (48 + 2), places=4)

    def test_non_resolved_outcomes_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "events.jsonl"
            ev = _outcome("smart_money", 0.6, "yes", won=True)
            ev["observed_side_price"] = 0.62  # não-extremo → não é proxy de resolução
            _write_events(log, [ev])
            self.assertEqual(build_probability_tables(log), {})

    def test_calibrated_lookup_requires_reliability(self):
        tables = {
            "s1": {
                "d7": {"n": 50, "realized": 0.31, "reliable": True},
                "d8": {"n": 5, "realized": 0.9, "reliable": False},
            }
        }
        self.assertAlmostEqual(calibrated_side_probability(tables, "s1", 0.75), 0.31)
        self.assertIsNone(calibrated_side_probability(tables, "s1", 0.85))  # n baixo
        self.assertIsNone(calibrated_side_probability(tables, "outra", 0.75))  # sem tabela


class CalibrationGateTest(unittest.IsolatedAsyncioTestCase):
    async def test_gate_blocks_negative_calibrated_edge(self):
        from settlement import _filter_and_rank_signals

        tables = {"weather_forecast": {"d8": {"n": 50, "realized": 0.10, "reliable": True}}}
        sig = {
            "strategy": "weather_forecast",
            "event_slug": "wx-1",
            "market_id": "1",
            "direction": "yes",
            "model_probability": 0.85,   # decil d8
            "market_probability": 0.40,  # preço do lado = 0.40 > realizado 0.10
            "edge": 0.45,
            "confidence": 0.7,
            "liquidity": 50000,
            "volume_24hr": 100000,
            "spread": 0.01,
        }
        actionable, rejected = await _filter_and_rank_signals(
            [sig], {"weather_forecast"}, True, 0.05, {},
            {"shadow_strategies": "", "min_net_edge": 0.0, "taker_fee_estimate": 0.0, "slippage_estimate": 0.0},
            tables,
        )
        self.assertEqual(actionable, [])
        self.assertTrue(any(r.get("reason") == "calibration_negative_edge" for r in rejected))

    async def test_unreliable_decile_passes_through(self):
        from settlement import _filter_and_rank_signals

        tables = {"weather_forecast": {"d8": {"n": 3, "realized": 0.10, "reliable": False}}}
        sig = {
            "strategy": "weather_forecast",
            "event_slug": "wx-2",
            "market_id": "2",
            "direction": "yes",
            "model_probability": 0.85,
            "market_probability": 0.40,
            "edge": 0.45,
            "confidence": 0.7,
            "liquidity": 50000,
            "volume_24hr": 100000,
            "spread": 0.01,
        }
        actionable, rejected = await _filter_and_rank_signals(
            [sig], {"weather_forecast"}, True, 0.05, {},
            {"shadow_strategies": "", "min_net_edge": 0.0, "taker_fee_estimate": 0.0, "slippage_estimate": 0.0},
            tables,
        )
        self.assertEqual(len(actionable), 1)
        self.assertNotIn("calibrated_edge", actionable[0])

    async def test_positive_calibrated_edge_kept_and_ranked(self):
        from settlement import _filter_and_rank_signals

        tables = {"weather_forecast": {"d8": {"n": 50, "realized": 0.80, "reliable": True}}}
        sig = {
            "strategy": "weather_forecast",
            "event_slug": "wx-3",
            "market_id": "3",
            "direction": "yes",
            "model_probability": 0.85,
            "market_probability": 0.40,  # lado 0.40 < realizado 0.80 → edge calibrado +0.40
            "edge": 0.45,
            "confidence": 0.7,
            "liquidity": 50000,
            "volume_24hr": 100000,
            "spread": 0.01,
        }
        actionable, _ = await _filter_and_rank_signals(
            [sig], {"weather_forecast"}, True, 0.05, {},
            {"shadow_strategies": "", "min_net_edge": 0.0, "taker_fee_estimate": 0.0, "slippage_estimate": 0.0},
            tables,
        )
        self.assertEqual(len(actionable), 1)
        self.assertAlmostEqual(actionable[0]["calibrated_edge"], 0.40)


if __name__ == "__main__":
    unittest.main()
