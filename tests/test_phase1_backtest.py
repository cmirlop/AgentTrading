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

    def test_ambiguous_filled_trade_pays_conservative_round_turn_costs(self):
        plan = dict(self.plan, volume=1.0)
        result = simulate_trade(
            plan, "2026-04-08T10:00:00Z", self.bars,
            cost_scenario="CONSERVATIVE", risk_amount_eur=100.0,
        )
        self.assertEqual(result["result"], "AMBIGUOUS_BOTH")
        self.assertEqual(result["commission_eur"], 7.0)
        self.assertEqual(result["slippage_points"], 2)
        self.assertAlmostEqual(result["transaction_cost_eur"], 7.2)
        self.assertIsNone(result["net_r"])

    def test_open_position_at_end_pays_only_entry_side_cost(self):
        plan = dict(self.plan, volume=1.0)
        bars = self.bars.copy()
        bars.loc[1, "high"] = 1.11
        bars.loc[1, "low"] = 1.095
        result = simulate_trade(
            plan, "2026-04-08T10:00:00Z", bars,
            cost_scenario="CONSERVATIVE", risk_amount_eur=100.0,
        )
        self.assertEqual(result["result"], "OPEN_AT_END")
        self.assertEqual(result["commission_eur"], 3.5)
        self.assertEqual(result["slippage_points"], 1)
        self.assertAlmostEqual(result["transaction_cost_eur"], 3.6)
        self.assertIsNone(result["net_r"])

    def test_cost_eur_is_unknown_if_slippage_cannot_be_converted(self):
        result = simulate_trade(
            self.plan, "2026-04-08T10:00:00Z", self.bars,
            cost_scenario="CONSERVATIVE", risk_amount_eur=None,
        )
        self.assertIsNone(result["transaction_cost_eur"])

    def test_buy_limit_uses_ask_and_spread_is_not_deducted_twice(self):
        plan = dict(self.plan, order_type="BUY_LIMIT", entry=1.1)
        bars = pd.DataFrame([
            {"time": pd.Timestamp("2026-04-08T10:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:05:00Z"), "high": 1.101, "low": 1.09999, "spread": 2},
            {"time": pd.Timestamp("2026-04-08T10:15:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:20:00Z"), "high": 1.101, "low": 1.09999, "spread": 2},
        ])
        result = simulate_trade(plan, "2026-04-08T10:00:00Z", bars, cost_scenario="BASE")
        self.assertEqual(result["result"], "PENDING_AT_END")
        self.assertEqual(result["spread_model"], "HISTORICAL_BID_ASK_EMBEDDED")
        self.assertEqual(result["transaction_cost_eur"], 0.0)

    def test_sell_limit_uses_bid_not_ask(self):
        plan = {
            "symbol": "EURUSD", "direction": "SELL", "order_type": "SELL_LIMIT",
            "entry": 1.1, "stop_loss": 1.11, "take_profit": 1.08,
            "target_rr": 2.0, "volume": 0.01,
        }
        bars = pd.DataFrame([
            {"time": pd.Timestamp("2026-04-08T10:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:05:00Z"), "high": 1.09999, "low": 1.095, "spread": 2},
            {"time": pd.Timestamp("2026-04-08T11:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T11:05:00Z"), "high": 1.09999, "low": 1.095, "spread": 2},
        ])
        result = simulate_trade(plan, "2026-04-08T10:00:00Z", bars)
        self.assertEqual(result["result"], "EXPIRED")

    def test_unfilled_order_expires_at_expiry_and_straddling_trigger_is_ambiguous(self):
        unfilled = pd.DataFrame([
            {"time": pd.Timestamp("2026-04-08T10:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T10:05:00Z"), "high": 1.099, "low": 1.095, "spread": 1},
            {"time": pd.Timestamp("2026-04-08T11:00:00Z"), "bar_close": pd.Timestamp("2026-04-08T11:05:00Z"), "high": 1.099, "low": 1.095, "spread": 1},
        ])
        expired = simulate_trade(self.plan, "2026-04-08T10:00:00Z", unfilled)
        self.assertEqual(expired["result"], "EXPIRED")

        straddling = pd.DataFrame([
            {"time": pd.Timestamp("2026-04-08T10:55:00Z"), "bar_close": pd.Timestamp("2026-04-08T11:00:00Z"), "high": 1.101, "low": 1.095, "spread": 1},
        ])
        ambiguous = simulate_trade(self.plan, "2026-04-08T10:00:00Z", straddling)
        self.assertEqual(ambiguous["result"], "AMBIGUOUS_EXPIRY_BAR")
        self.assertIsNone(ambiguous["gross_r"])

    def test_incomplete_history_before_expiry_stays_pending_at_end(self):
        bars = self.bars.iloc[:1].copy()
        bars.loc[0, "high"] = 1.099
        bars.loc[0, "low"] = 1.095
        result = simulate_trade(self.plan, "2026-04-08T10:00:00Z", bars)
        self.assertEqual(result["result"], "PENDING_AT_END")

    def test_unsorted_bars_are_rejected_before_simulation(self):
        bars = self.bars.iloc[::-1].reset_index(drop=True)
        with self.assertRaisesRegex(ValueError, "HISTORICAL_BARS_NOT_STRICTLY_CHRONOLOGICAL"):
            simulate_trade(self.plan, "2026-04-08T10:00:00Z", bars)


if __name__ == "__main__":
    unittest.main()
