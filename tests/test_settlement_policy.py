import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from settlement import DEFAULT_EXECUTION_POLICY, apply_execution_filters


class ExecutionPolicyTest(unittest.TestCase):
    def test_smart_money_passes_with_liquid_tight_market(self):
        accepted, rejected = apply_execution_filters(
            [
                {
                    "strategy": "smart_money",
                    "event_slug": "m1",
                    "direction": "yes",
                    "market_probability": 0.55,
                    "edge": 0.08,
                    "spread": 0.02,
                    "liquidity": 20000,
                    "volume_24hr": 80000,
                }
            ],
            DEFAULT_EXECUTION_POLICY,
        )

        self.assertEqual(len(accepted), 1)
        self.assertEqual(rejected, [])
        self.assertGreaterEqual(accepted[0]["net_edge"], DEFAULT_EXECUTION_POLICY["min_net_edge"])

    def test_arbitrage_is_shadow_by_default(self):
        accepted, rejected = apply_execution_filters(
            [
                {
                    "strategy": "arbitrage",
                    "event_slug": "arb",
                    "direction": "yes",
                    "market_probability": 0.5,
                    "edge": 0.20,
                    "spread": 0.0,
                    "liquidity": 100000,
                    "volume_24hr": 100000,
                }
            ],
            DEFAULT_EXECUTION_POLICY,
        )

        self.assertEqual(accepted, [])
        self.assertEqual(len(rejected), 1)
        self.assertIn("shadow", rejected[0]["reason"])


if __name__ == "__main__":
    unittest.main()
