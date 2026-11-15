from __future__ import annotations

import unittest

import pandas as pd

from src.phase2_2_2c_analysis import cost_conversion
from src.backtest_cost_audit import convert_cost_to_eur
from src.spread_quality import audit_spread_values, classify_spread


class Phase222eCostAuditTests(unittest.TestCase):
    def test_spread_points_price_conversion_and_double_charge_guard(self):
        cost = cost_conversion(5, 0.00001, 0.1, 7, 1)
        self.assertAlmostEqual(cost["spread_price"], 0.00005)
        self.assertTrue(cost["spread_embedded_in_bid_ask"])
        self.assertEqual(cost["additional_spread_cost"], 0.0)
        self.assertFalse(cost["spread_double_counted"])
        self.assertEqual(
            cost["eur_pnl_conversion"],
            "NOT_CALCULATED_WITHOUT_VERIFIED_CONTRACT_AND_CURRENCY_CONVERSION",
        )

    def test_zero_spread_is_unverified_and_scenario_values_remain_hypotheses(self):
        self.assertEqual(classify_spread(0), "OBSERVED_ZERO_UNVERIFIED")
        audit = audit_spread_values(pd.Series([0, 2, None, -1]))
        self.assertEqual(audit["quality_counts"]["OBSERVED_ZERO_UNVERIFIED"], 1)
        self.assertEqual(audit["quality_counts"]["MISSING"], 1)
        self.assertEqual(audit["quality_counts"]["INVALID"], 1)

    def test_missing_eur_cross_is_unverified_and_future_rate_is_rejected(self):
        missing = convert_cost_to_eur(2.0, None, rate_available_utc=None, decision_time_utc="2026-01-01T00:00:00Z")
        self.assertEqual(missing["status"], "COST_CONVERSION_UNVERIFIED")
        self.assertIsNone(missing["amount_eur"])
        future = convert_cost_to_eur(
            2.0, 0.9, rate_available_utc="2026-01-01T00:01:00Z",
            decision_time_utc="2026-01-01T00:00:00Z",
        )
        self.assertEqual(future["status"], "COST_CONVERSION_LOOKAHEAD")
        self.assertIsNone(future["amount_eur"])
        verified = convert_cost_to_eur(
            2.0, 0.9, rate_available_utc="2025-12-31T23:59:00Z",
            decision_time_utc="2026-01-01T00:00:00Z",
        )
        self.assertEqual(verified["status"], "PASS")
        self.assertEqual(verified["amount_eur"], 1.8)


if __name__ == "__main__":
    unittest.main()
