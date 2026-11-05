import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.historical_execution import simulate_trade


class HistoricalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "symbol": "EURUSD", "direction": "BUY", "order_type": "BUY_STOP",
            "entry": 1.1000, "stop_loss": 1.0900, "take_profit": 1.1200,
            "target_rr": 2.0, "volume": 0.01,
        }
        self.bars = pd.DataFrame([
            {"time": pd.Timestamp("2026-04-08T10:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:05:00Z"), "high": 1.1010, "low": 1.0950, "spread": 1},
            {"time": pd.Timestamp("2026-04-08T10:05:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:10:00Z"), "high": 1.1300, "low": 1.0800, "spread": 1},
        ])

    def test_simultaneous_sl_tp_is_ambiguous(self):
        result = simulate_trade(self.plan, "2026-04-08T10:00:00Z", self.bars)
        self.assertEqual(result["result"], "AMBIGUOUS_BOTH")
        self.assertIsNone(result["r"])

    def test_transaction_cost_scenarios_are_explicit_and_reduce_net_r(self):
        bars = self.bars.copy()
        bars.loc[1, "high"] = 1.1210
        bars.loc[1, "low"] = 1.0950
        result = simulate_trade(
            self.plan, "2026-04-08T10:00:00Z", bars,
            cost_scenario="CONSERVATIVE", risk_amount_eur=10.0,
        )
        self.assertEqual(result["result"], "WIN")
        self.assertLess(result["net_r"], result["gross_r"])
        self.assertGreater(result["transaction_cost_eur"], 0)
        self.assertAlmostEqual(
            result["net_r"] * 10.0,
            result["gross_r"] * 10.0 - result["transaction_cost_eur"],
        )
        self.assertIn("commission=7 EUR/lot", result["assumptions_unverified"])

    def test_invalid_cost_scenario_and_missing_spread_rejected(self):
        with self.assertRaisesRegex(ValueError, "UNKNOWN_COST_SCENARIO"):
            simulate_trade(self.plan, "2026-04-08T10:00:00Z", self.bars, cost_scenario="CHEAP")
        with self.assertRaisesRegex(ValueError, "HISTORICAL_SPREAD_UNAVAILABLE"):
            simulate_trade(self.plan, "2026-04-08T10:00:00Z", self.bars.drop(columns=["spread"]))

    def test_data_after_pending_expiry_does_not_affect_result(self):
        bars = pd.DataFrame([
            {"time": pd.Timestamp("2026-04-08T10:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:05:00Z"), "high": 1.0, "low": 0.99, "spread": 1},
            {"time": pd.Timestamp("2026-04-08T11:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T11:05:00Z"), "high": 1.2, "low": 0.9, "spread": float("nan")},
        ])
        result = simulate_trade(self.plan, "2026-04-08T10:00:00Z", bars)
        self.assertEqual(result["result"], "EXPIRED")

    def test_same_input_reproduces_same_result(self):
        one = simulate_trade(self.plan, "2026-04-08T10:00:00Z", self.bars, cost_scenario="STRESS", risk_amount_eur=10)
        two = simulate_trade(self.plan, "2026-04-08T10:00:00Z", self.bars, cost_scenario="STRESS", risk_amount_eur=10)
        self.assertEqual(one, two)

    def test_stress_scenario_has_higher_explicit_costs(self):
        conservative = simulate_trade(
            self.plan, "2026-04-08T10:00:00Z", self.bars,
            cost_scenario="CONSERVATIVE", risk_amount_eur=10,
        )
        stress = simulate_trade(
            self.plan, "2026-04-08T10:00:00Z", self.bars,
            cost_scenario="STRESS", risk_amount_eur=10,
        )
        self.assertGreater(stress["transaction_cost_eur"], conservative["transaction_cost_eur"])


if __name__ == "__main__":
    unittest.main()
