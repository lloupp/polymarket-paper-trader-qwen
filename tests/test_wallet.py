import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wallet import Wallet


def fresh_wallet(path: str) -> Wallet:
    wallet = Wallet(path)
    wallet.state.update(
        {
            "bankroll": 10000.0,
            "initial_bankroll": 10000.0,
            "positions": {},
            "history": [],
            "cooldowns": {},
            "settings": {
                "auto_risk_enabled": False,
                "stop_loss": 0.20,
                "take_profit": 0.25,
                "max_trade": 100.0,
                "max_exposure": 500.0,
                "min_trade": 1.0,
                "max_per_scan": 10,
                "min_edge": 0.05,
            },
        }
    )
    wallet.save()
    return wallet


class WalletAccountingTest(unittest.TestCase):
    def test_open_position_uses_size_as_dollar_stake(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))

            pos = wallet.open_position("m1", "YES", price=0.4, size=20.0)

            self.assertEqual(pos["cost"], 20.0)
            self.assertEqual(pos["size"], 20.0)
            self.assertAlmostEqual(pos["shares"], 50.0)
            self.assertEqual(wallet.get_total_exposure(), 20.0)
            self.assertEqual(wallet.get_available_bankroll(), 9980.0)

    def test_yes_close_returns_side_price_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))
            pos = wallet.open_position("m1", "YES", price=0.4, size=20.0)

            closed = wallet.close_position(pos["id"], close_price=0.5)

            self.assertAlmostEqual(closed["pnl"], 5.0)
            self.assertAlmostEqual(wallet.state["bankroll"], 10005.0)

    def test_no_pnl_uses_no_side_price_not_inverse_yes_formula(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))
            pos = wallet.open_position("m1", "NO", price=0.4, size=20.0)

            closed = wallet.close_position(pos["id"], close_price=0.55)

            self.assertAlmostEqual(closed["pnl"], 7.5)
            self.assertAlmostEqual(wallet.state["bankroll"], 10007.5)

    def test_legacy_no_position_without_shares_is_supported(self):
        pnl, pnl_pct = Wallet.calculate_pnl(
            {
                "side": "NO",
                "entry_price": 0.4,
                "size": 50.0,
                "cost": 20.0,
            },
            current_price=0.55,
        )

        self.assertAlmostEqual(pnl, 7.5)
        self.assertAlmostEqual(pnl_pct, 0.375)


class StrategyExitOverridesTest(unittest.TestCase):
    # fresh_wallet não define strategy_exits → vale o default de DEFAULT_SETTINGS
    # (btc_5m_momentum com take_profit desabilitado, hold até resolução).

    def test_btc_5m_winner_is_not_closed_by_take_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))
            pos = wallet.open_position("m1", "YES", price=0.4, size=20.0,
                                       extra={"strategy": "btc_5m_momentum"})
            # +50% de lucro: TP global (0.25) fecharia, mas o override desliga o TP
            self.assertIsNone(wallet.check_risk_exit(pos["id"], current_price=0.6))

    def test_btc_5m_stop_loss_still_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))
            pos = wallet.open_position("m1", "YES", price=0.4, size=20.0,
                                       extra={"strategy": "btc_5m_momentum"})
            closed = wallet.check_risk_exit(pos["id"], current_price=0.3)  # -25%
            self.assertIsNotNone(closed)
            self.assertEqual(closed["close_reason"], "stop_loss")

    def test_strategy_without_override_keeps_global_take_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))
            pos = wallet.open_position("m1", "YES", price=0.4, size=20.0,
                                       extra={"strategy": "smart_money"})
            closed = wallet.check_risk_exit(pos["id"], current_price=0.52)  # +30% >= 0.25
            self.assertIsNotNone(closed)
            self.assertEqual(closed["close_reason"], "take_profit")

    def test_wallet_settings_override_beats_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            wallet = fresh_wallet(str(Path(tmp) / "wallet.json"))
            wallet.state["settings"]["strategy_exits"] = {"btc_5m_momentum": {"take_profit": 0.10}}
            pos = wallet.open_position("m1", "YES", price=0.4, size=20.0,
                                       extra={"strategy": "btc_5m_momentum"})
            closed = wallet.check_risk_exit(pos["id"], current_price=0.6)  # +50% >= 0.10
            self.assertIsNotNone(closed)
            self.assertEqual(closed["close_reason"], "take_profit")


if __name__ == "__main__":
    unittest.main()
