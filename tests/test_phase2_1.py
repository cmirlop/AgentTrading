"""Offline synthetic tests for Phase 2.1 evaluation helpers."""

import ast
from pathlib import Path
import unittest

import pandas as pd

from src.phase2_1_evaluation import (
    assert_not_test_for_tuning,
    attach_event_ids,
    chronological_split,
    pair_on_event_id,
    purge_trade_events,
    stable_event_id,
    summarize_trades,
    validate_llm_provenance,
    weekly_block_bootstrap_mean,
)


ROOT = Path(__file__).resolve().parents[1]


class Phase2DataTests(unittest.TestCase):
    def test_chronological_split_is_ordered_and_embargoed(self):
        rows = [
            {"symbol": "EURUSD", "analysis_time": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(days=day, hours=12)}
            for day in range(40)
        ]
        assigned, plan = chronological_split(pd.DataFrame(rows), embargo_hours=24)
        day_to_period = assigned.assign(day=pd.to_datetime(assigned.analysis_time, utc=True).dt.date).groupby("day").period.nunique()
        self.assertTrue((day_to_period == 1).all())
        train_days = set(assigned.loc[assigned.period == "TRAIN", "analysis_time"].dt.date)
        validation_days = set(assigned.loc[assigned.period == "VALIDATION", "analysis_time"].dt.date)
        test_days = set(assigned.loc[assigned.period == "TEST", "analysis_time"].dt.date)
        self.assertFalse(train_days & validation_days)
        self.assertFalse(train_days & test_days)
        self.assertFalse(validation_days & test_days)
        self.assertEqual(plan["embargo_hours"], 24)
        self.assertGreater(int((assigned.period == "EMBARGO").sum()), 0)

    def test_split_rejects_timezone_naive_timestamps(self):
        frame = pd.DataFrame({"analysis_time": ["2026-01-01 12:00:00"] * 6})
        with self.assertRaisesRegex(ValueError, "TIMEZONE_MISSING"):
            chronological_split(frame)

    def test_stable_event_id_ignores_future_outcome_fields(self):
        event = {"symbol": "eurusd", "analysis_time": "2026-01-01T12:00:00+00:00", "bias": "BULLISH", "future_pnl": -100}
        changed_outcome = dict(event, future_pnl=1000, actual_outcome="WIN")
        self.assertEqual(stable_event_id(event), stable_event_id(changed_outcome))

    def test_event_ids_are_unique_on_distinct_symbol_closes(self):
        frame = pd.DataFrame([
            {"symbol": "EURUSD", "analysis_time": "2026-01-01T12:00:00+00:00"},
            {"symbol": "USDCAD", "analysis_time": "2026-01-01T12:00:00+00:00"},
            {"symbol": "EURUSD", "analysis_time": "2026-01-01T12:15:00+00:00"},
        ])
        with_ids = attach_event_ids(frame, "analysis_time")
        self.assertEqual(with_ids.event_id.nunique(), 3)

    def test_event_pairing_uses_only_common_ids(self):
        technical = pd.DataFrame({"event_id": ["a", "b"], "pnl": [1.0, -1.0]})
        llm = pd.DataFrame({"event_id": ["b", "c"], "decision": ["HOLD", "BUY"]})
        paired = pair_on_event_id(technical, llm)
        self.assertEqual(paired.event_id.tolist(), ["b"])

    def test_provenance_rejects_unattributed_model_decision(self):
        valid, missing = validate_llm_provenance({"symbol": "EURUSD", "timestamp": "2026-01-01T00:00:00Z", "response": {"action": "HOLD"}})
        self.assertFalse(valid)
        self.assertIn("provider", missing)
        complete = {
            "decision_id": "d1", "symbol": "EURUSD", "timestamp": "2026-01-01T00:00:00Z",
            "provider": "deepseek", "model": "deepseek-flash", "prompt_version": "p1",
            "payload_hash": "abc", "response": {"action": "HOLD"}, "validation_status": "VALID", "evidence_ids": ["e1"],
        }
        self.assertTrue(validate_llm_provenance(complete)[0])

    def test_test_partition_is_protected_from_tuning(self):
        with self.assertRaisesRegex(ValueError, "TEST_PARTITION_IS_RESERVED"):
            assert_not_test_for_tuning("TEST")
        assert_not_test_for_tuning("TRAIN")

    def test_embargo_and_cross_partition_trades_are_purged(self):
        split = {
            "train_end_exclusive_utc": "2026-01-10T00:00:00+00:00",
            "test_start_utc": "2026-01-20T00:00:00+00:00",
            "dataset_end_exclusive_utc": "2026-01-31T00:00:00+00:00",
            "embargo_hours": 24,
        }
        trades = pd.DataFrame([
            {"signal_time": "2026-01-07T12:00:00Z", "release_time": "2026-01-08T12:00:00Z"},
            {"signal_time": "2026-01-08T12:00:00Z", "release_time": "2026-01-11T00:00:00Z"},
            {"signal_time": "2026-01-10T12:00:00Z", "release_time": "2026-01-11T12:00:00Z"},
            {"signal_time": "2026-01-22T12:00:00Z", "release_time": "2026-01-23T12:00:00Z"},
        ])
        result, counts = purge_trade_events(trades, split)
        self.assertEqual(result.period.tolist(), ["TRAIN", "PURGED", "EMBARGO", "TEST"])
        self.assertEqual(counts["EVENT_CROSSES_PARTITION_OR_EMBARGO"], 1)


class Phase2StatisticsTests(unittest.TestCase):
    def test_ambiguous_and_open_trades_are_not_in_net_expectancy(self):
        frame = pd.DataFrame([
            {"result": "WIN", "gross_pnl_eur": 10.0, "transaction_cost_eur": 2.0, "net_pnl_eur": 8.0, "net_r": 0.8, "fill_time": "2026-01-01T00:00:00Z", "exit_time": "2026-01-01T01:00:00Z"},
            {"result": "LOSS", "gross_pnl_eur": -10.0, "transaction_cost_eur": 2.0, "net_pnl_eur": -12.0, "net_r": -1.2, "fill_time": "2026-01-02T00:00:00Z", "exit_time": "2026-01-02T01:00:00Z"},
            {"result": "AMBIGUOUS_BOTH", "gross_pnl_eur": None, "transaction_cost_eur": 1.0, "net_pnl_eur": None, "net_r": None, "fill_time": "2026-01-03T00:00:00Z", "exit_time": "2026-01-03T01:00:00Z"},
            {"result": "OPEN_AT_END", "gross_pnl_eur": None, "transaction_cost_eur": 1.0, "net_pnl_eur": None, "net_r": None, "fill_time": "2026-01-04T00:00:00Z", "exit_time": "2026-01-04T01:00:00Z"},
        ])
        summary = summarize_trades(frame)
        self.assertEqual(summary["proposed"], 4)
        self.assertEqual(summary["closed"], 2)
        self.assertEqual(summary["ambiguous"], 1)
        self.assertEqual(summary["open_or_pending"], 1)
        self.assertEqual(summary["net_pnl_eur"], -4.0)
        self.assertEqual(summary["costs_eur_closed"], 4.0)
        self.assertAlmostEqual(summary["expectancy_net_r"], -0.2)

    def test_costs_are_not_double_subtracted_when_net_pnl_is_present(self):
        frame = pd.DataFrame([{"result": "WIN", "gross_pnl_eur": 10.0, "transaction_cost_eur": 2.0, "net_pnl_eur": 8.0, "fill_time": "2026-01-01T00:00:00Z", "exit_time": "2026-01-01T01:00:00Z"}])
        self.assertEqual(summarize_trades(frame)["net_pnl_eur"], 8.0)

    def test_position_overlap_exposure_is_aggregated_from_fills(self):
        frame = pd.DataFrame([
            {"result": "WIN", "gross_pnl_eur": 10.0, "net_pnl_eur": 10.0, "estimated_risk_eur": 10.0, "fill_time": "2026-01-01T00:00:00Z", "exit_time": "2026-01-01T02:00:00Z"},
            {"result": "LOSS", "gross_pnl_eur": -5.0, "net_pnl_eur": -5.0, "estimated_risk_eur": 5.0, "fill_time": "2026-01-01T01:00:00Z", "exit_time": "2026-01-01T03:00:00Z"},
        ])
        metrics = summarize_trades(frame)
        self.assertEqual(metrics["time_in_market_hours"], 3.0)
        self.assertEqual(metrics["max_simultaneous_filled_positions"], 2)
        self.assertEqual(metrics["max_open_risk_eur"], 15.0)

    def test_weekly_block_bootstrap_is_reproducible_with_seed(self):
        rows = []
        for week in range(12):
            for day in range(2):
                rows.append({"result": "WIN" if day == 0 else "LOSS", "net_pnl_eur": float(week + day + 1), "exit_time": pd.Timestamp("2026-01-05", tz="UTC") + pd.Timedelta(weeks=week, days=day)})
        frame = pd.DataFrame(rows)
        first = weekly_block_bootstrap_mean(frame, seed=77, repetitions=300)
        second = weekly_block_bootstrap_mean(frame, seed=77, repetitions=300)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "PASS")
        self.assertEqual(first["blocks"], 12)

    def test_weekly_block_bootstrap_marks_too_few_blocks_insufficient(self):
        frame = pd.DataFrame([{"result": "WIN", "net_pnl_eur": 1.0, "exit_time": f"2026-01-{day:02d}T12:00:00Z"} for day in range(1, 8)])
        result = weekly_block_bootstrap_mean(frame)
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertIsNone(result["ci95"])

    def test_phase2_code_has_no_live_provider_or_order_calls(self):
        paths = [ROOT / "src/phase2_1_evaluation.py", ROOT / "scripts/validate_phase2_1.py"]
        forbidden_imports = {"MetaTrader5", "openai", "ollama", "requests", "httpx", "urllib"}
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertFalse({alias.name.split(".")[0] for alias in node.names} & forbidden_imports)
                elif isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[0], forbidden_imports)
                elif isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
                    self.assertNotIn(name.lower(), {"order_send", "place_order", "send_order", "create_order"})


if __name__ == "__main__":
    unittest.main()
