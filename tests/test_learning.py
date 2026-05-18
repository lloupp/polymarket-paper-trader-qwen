import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from learning import (
    append_trade_feature,
    build_policy_recommendations,
    compute_learning_metrics,
    default_learning_state,
    ensure_learning_state,
    learning_snapshot,
    maybe_refresh_policy,
)


class LearningPolicyTest(unittest.TestCase):
    def test_ensure_learning_state_repairs_missing_or_invalid_fields(self):
        wallet_state = {"learning_state": {"trades_features": "bad", "strategy_stats": []}}

        ls = ensure_learning_state(wallet_state)

        self.assertIs(wallet_state["learning_state"], ls)
        self.assertIsInstance(ls["trades_features"], list)
        self.assertIsInstance(ls["strategy_stats"], dict)
        self.assertIsInstance(ls["edge_buckets_stats"], dict)
        self.assertIn("policy_recommendations", ls)

    def test_metrics_compute_winrate_pnl_and_hold_time(self):
        ls = default_learning_state()
        for pnl, hold in [(2.0, 5.0), (-1.0, 15.0), (3.0, 10.0)]:
            append_trade_feature(
                ls,
                {
                    "strategy": "smart_money",
                    "edge_bucket": "0.08_0.12",
                    "pnl": pnl,
                    "win": 1 if pnl > 0 else 0,
                    "close_reason": "take_profit" if pnl > 0 else "stop_loss",
                    "hold_minutes": hold,
                },
            )

        metrics = compute_learning_metrics(ls)
        smart = metrics["strategy_stats"]["smart_money"]

        self.assertEqual(smart["n"], 3)
        self.assertEqual(smart["wins"], 2)
        self.assertAlmostEqual(smart["avg_pnl"], 4.0 / 3.0, places=6)
        self.assertAlmostEqual(smart["avg_hold_minutes"], 10.0)
        self.assertAlmostEqual(smart["stop_loss_rate"], 1.0 / 3.0, places=6)

    def test_policy_raises_min_edge_and_reduces_bad_strategy_size(self):
        ls = default_learning_state()
        ls["min_samples"] = 3
        settings = {"min_edge": 0.05}

        for _ in range(3):
            append_trade_feature(
                ls,
                {
                    "strategy": "smart_money",
                    "edge_bucket": "0.05_0.08",
                    "pnl": -1.0,
                    "win": 0,
                    "close_reason": "stop_loss",
                    "hold_minutes": 6.0,
                },
            )
        for _ in range(3):
            append_trade_feature(
                ls,
                {
                    "strategy": "event_countdown",
                    "edge_bucket": "0.12_0.20",
                    "pnl": 2.0,
                    "win": 1,
                    "close_reason": "take_profit",
                    "hold_minutes": 4.0,
                },
            )

        compute_learning_metrics(ls)
        rec = build_policy_recommendations(ls, settings)

        self.assertGreater(rec["effective_min_edge"], settings["min_edge"])
        self.assertEqual(rec["strategy_multipliers"]["smart_money"], 0.85)
        self.assertEqual(rec["strategy_multipliers"]["event_countdown"], 1.10)
        self.assertEqual(rec["confidence"], "medium")

    def test_maybe_refresh_policy_updates_snapshot(self):
        ls = default_learning_state()
        ls["min_samples"] = 3
        for _ in range(3):
            append_trade_feature(
                ls,
                {
                    "strategy": "smart_money",
                    "edge_bucket": "0.05_0.08",
                    "pnl": -1.0,
                    "win": 0,
                    "close_reason": "stop_loss",
                },
            )

        maybe_refresh_policy(ls, {"min_edge": 0.05})
        snapshot = learning_snapshot(ls)

        self.assertEqual(snapshot["features_count"], 3)
        self.assertEqual(snapshot["strategies_tracked"], 1)
        self.assertGreater(snapshot["effective_min_edge"], 0.05)


if __name__ == "__main__":
    unittest.main()
