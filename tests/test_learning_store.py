import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from learning_store import (
    observe_signal_outcomes_from_signals,
    read_jsonl,
    record_signal_decisions,
    summarize_learning_events,
)


class LearningStoreTest(unittest.TestCase):
    def test_records_decisions_and_pending_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "events.jsonl"
            pending_file = Path(tmp) / "pending.json"
            signals = [
                {
                    "strategy": "smart_money",
                    "market_id": "m1",
                    "event_slug": "slug-1",
                    "direction": "yes",
                    "market_probability": 0.55,
                    "edge": 0.08,
                    "confidence": 0.7,
                },
                {
                    "strategy": "value",
                    "market_id": "m2",
                    "event_slug": "slug-2",
                    "direction": "yes",
                    "market_probability": 0.50,
                    "edge": 0.04,
                },
            ]
            accepted = [dict(signals[0], net_edge=0.06)]
            executed = [{"strategy": "smart_money", "market_id": "m1", "direction": "yes"}]

            summary = record_signal_decisions(
                cycle_id="cycle-1",
                signals=signals,
                selected_strategies={"smart_money", "value"},
                effective_min_edge=0.05,
                accepted_signals=accepted,
                policy_rejected=[],
                top_signals=accepted,
                executed=executed,
                skipped=[],
                event_log=event_log,
                pending_file=pending_file,
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                outcome_horizon_minutes=30,
            )

            rows = read_jsonl(event_log)
            pending = json.loads(pending_file.read_text())

            self.assertEqual(summary["decision_events"], 2)
            self.assertEqual(summary["decision_counts"]["executed"], 1)
            self.assertEqual(summary["decision_counts"]["rejected"], 1)
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(pending["signals"]), 2)
            self.assertEqual(rows[0]["event_type"], "signal_decision")
            self.assertEqual(rows[0]["decision"], "executed")
            self.assertEqual(rows[1]["stage"], "learning_min_edge")

    def test_repeated_signal_across_cycles_keeps_single_pending_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "events.jsonl"
            pending_file = Path(tmp) / "pending.json"
            signal = {
                "strategy": "weather_forecast",
                "market_id": "m1",
                "event_slug": "slug-1",
                "direction": "yes",
                "market_probability": 0.40,
                "edge": 0.10,
            }
            for cycle in ("cycle-1", "cycle-2", "cycle-3"):
                record_signal_decisions(
                    cycle_id=cycle,
                    signals=[dict(signal)],
                    selected_strategies={"weather_forecast"},
                    effective_min_edge=0.05,
                    accepted_signals=[],
                    policy_rejected=[],
                    top_signals=[],
                    executed=[],
                    skipped=[],
                    event_log=event_log,
                    pending_file=pending_file,
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    outcome_horizon_minutes=30,
                )
            pending = json.loads(pending_file.read_text())
            rows = read_jsonl(event_log)
            # Every cycle logs its decision, but only one outcome observation
            # stays in flight per signal_key (dedup prevents buffer flooding).
            self.assertEqual(len(rows), 3)
            self.assertEqual(len(pending["signals"]), 1)

    def test_observes_due_pending_signal_outcome_from_later_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "events.jsonl"
            pending_file = Path(tmp) / "pending.json"
            initial_signal = {
                "strategy": "smart_money",
                "market_id": "m1",
                "event_slug": "slug-1",
                "direction": "yes",
                "market_probability": 0.50,
                "edge": 0.08,
            }
            current_signal = dict(initial_signal, market_probability=0.60)

            record_signal_decisions(
                cycle_id="cycle-1",
                signals=[initial_signal],
                selected_strategies={"smart_money"},
                effective_min_edge=0.05,
                accepted_signals=[dict(initial_signal, net_edge=0.06)],
                policy_rejected=[],
                top_signals=[],
                executed=[],
                skipped=[],
                event_log=event_log,
                pending_file=pending_file,
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                outcome_horizon_minutes=0,
            )

            summary = observe_signal_outcomes_from_signals(
                [current_signal],
                event_log=event_log,
                pending_file=pending_file,
                now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
            )
            rows = read_jsonl(event_log)
            pending = json.loads(pending_file.read_text())

            self.assertEqual(summary["outcome_events"], 1)
            self.assertEqual(len(pending["signals"]), 0)
            outcome = rows[-1]
            self.assertEqual(outcome["event_type"], "signal_outcome")
            self.assertEqual(outcome["win"], 1)
            self.assertAlmostEqual(outcome["paper_pnl_per_share"], 0.10)

    def test_summarizes_recent_learning_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            event_log = Path(tmp) / "events.jsonl"
            pending_file = Path(tmp) / "pending.json"
            signal = {
                "strategy": "event_countdown",
                "market_id": "m1",
                "direction": "no",
                "market_probability": 0.70,
                "edge": 0.10,
            }
            later = dict(signal, market_probability=0.60)

            record_signal_decisions(
                cycle_id="cycle-1",
                signals=[signal],
                selected_strategies={"event_countdown"},
                effective_min_edge=0.05,
                accepted_signals=[dict(signal, net_edge=0.07)],
                policy_rejected=[],
                top_signals=[],
                executed=[],
                skipped=[],
                event_log=event_log,
                pending_file=pending_file,
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                outcome_horizon_minutes=0,
            )
            observe_signal_outcomes_from_signals(
                [later],
                event_log=event_log,
                pending_file=pending_file,
                now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
            )

            summary = summarize_learning_events(event_log)

            self.assertEqual(summary["decision_events"], 1)
            self.assertEqual(summary["outcome_events"], 1)
            self.assertEqual(summary["decision_counts"]["not_selected"], 1)
            self.assertEqual(summary["outcome_by_strategy"]["event_countdown"]["n"], 1)


if __name__ == "__main__":
    unittest.main()
