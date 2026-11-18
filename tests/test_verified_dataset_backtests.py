from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import unittest
from unittest.mock import patch

import pandas as pd

from config.market_config import SYMBOLS, TIMEFRAMES
from src.historical_execution import simulate_trade
from src.historical_state import build_historical_market_state
from src.historical_trade_planner import build_historical_trade_plan
from src.verified_research_dataset import (
    VerifiedResearchDataset,
    validate_scan_dataset_fingerprint,
)


UTC = timezone.utc
DECISION = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
SYNTHETIC_SHA = "541a847e9443bbba10a298cd23e11e5d5db73d6b8af758c72a63e1bfa61e3984"
SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


def synthetic_session_manifest():
    weekly = {str(day): [{"start_local": "00:00", "end_local": "24:00"}] for day in range(7)}
    reference = {
        "byte_start": 0, "byte_end": 363, "excerpt_sha256": SYNTHETIC_SHA,
    }
    return {
        "schema_version": 2, "rule_version_id": "synthetic-test-v1",
        "evidence": {
            "status": "OFFICIAL_BROKER_DOCUMENTED", "official_source": True,
            "source_url": "https://broker.example.test/sessions.txt",
            "sha256": SYNTHETIC_SHA, "document_sha256": SYNTHETIC_SHA,
            "document_path": "tests/fixtures/broker_calendar_synthetic.txt",
            "rule_references": {field: [reference] for field in (
                "symbols", "timeframes", "timezone_rule", "weekly_sessions", "exceptions",
                "timeframe_boundary_policy", "effective_range", "exception_coverage",
            )},
            "accessed_utc": "2026-01-01T00:00:00Z",
            "effective_from_utc": "2020-01-01T00:00:00Z", "effective_to_utc": "2030-01-01T00:00:00Z",
        },
        "symbols": list(SYMBOLS), "timeframes": list(TIMEFRAMES),
        "timezone_rule": {"mode": "IANA", "name": "UTC"},
        "sessions_complete": True, "exceptions_complete": True,
        "exception_coverage": {"from_local_date": "2020-01-01", "through_local_date": "2030-01-01"},
        "weekly_sessions": weekly, "exceptions": {},
        "timeframe_boundary_policy": {**{tf: "FIXED_UTC" for tf in TIMEFRAMES}, "D1": "SERVER_LOCAL_DAILY"},
    }


def synthetic_frames():
    frames = {}
    for symbol in SYMBOLS:
        frames[symbol] = {}
        for timeframe in TIMEFRAMES:
            duration = SECONDS[timeframe]
            anchor = DECISION
            if timeframe == "D1":
                anchor = DECISION.replace(hour=0, minute=0, second=0, microsecond=0)
            elif timeframe == "H4":
                anchor = DECISION.replace(hour=12, minute=0, second=0, microsecond=0)
            records = []
            for index in range(692):
                close_time = anchor - timedelta(seconds=duration * (691 - index))
                open_time = close_time - timedelta(seconds=duration)
                price = 1.10 + index * 0.00001 + math.sin(index / 8.0) * 0.0004
                records.append({
                    "time": open_time, "bar_close": close_time,
                    "open": price - 0.00005, "high": price + 0.00035,
                    "low": price - 0.00035, "close": price,
                    "tick_volume": 100.0, "spread": 2.0, "real_volume": 0.0,
                })
            if timeframe == "M5":
                for offset in (0, 300, 600, 900):
                    open_time = DECISION + timedelta(seconds=offset)
                    price = 1.108
                    records.append({
                        "time": open_time, "bar_close": open_time + timedelta(seconds=300),
                        "open": price, "high": 2.0, "low": 0.1, "close": price,
                        "tick_volume": 100.0, "spread": 2.0, "real_volume": 0.0,
                    })
            frames[symbol][timeframe] = pd.DataFrame(records)
    return frames


class VerifiedDatasetBacktestTests(unittest.TestCase):
    def setUp(self):
        self.trusted_source = patch(
            "src.phase2_2_2d_sessions.TRUSTED_BROKER_SOURCE_HOSTS",
            frozenset({"broker.example.test"}),
        )
        self.trusted_source.start()
        self.addCleanup(self.trusted_source.stop)
        self.dataset = VerifiedResearchDataset.synthetic_test_dataset(
            synthetic_frames(), synthetic_session_manifest(),
            warmup_start_utc="2024-01-01T00:00:00Z",
            evaluation_start_utc=DECISION,
            evaluation_end_utc=DECISION + timedelta(seconds=1),
            observation_end_utc=DECISION + timedelta(minutes=15),
            as_of_utc=DECISION + timedelta(minutes=15),
        )

    def test_scan_and_backtest_consume_the_same_hashed_frames_end_to_end(self):
        bundle = self.dataset.scan_symbol("EURUSD")
        self.dataset.assert_scan_parity(bundle, symbol="EURUSD")
        self.assertEqual(len(bundle["rows"]), 1)
        self.assertEqual(bundle["rows"].iloc[0]["analysis_time"], pd.Timestamp(DECISION))
        prepared = self.dataset.prepare_backtest_history("EURUSD")
        state = build_historical_market_state("EURUSD", DECISION, prepared)
        plan = build_historical_trade_plan("EURUSD", "BUY", "STOP", state)
        self.assertEqual(plan["status"], "VALID")
        execution = simulate_trade(
            plan, DECISION, prepared["M5"], cost_scenario="BASE",
            risk_amount_eur=100.0, spread_floor_points=0.0,
        )
        self.assertIn(execution["result"], {"AMBIGUOUS_ENTRY_BAR", "AMBIGUOUS_BOTH", "WIN", "LOSS"})
        self.assertEqual(execution["spread_model"], "HISTORICAL_BID_ASK_EMBEDDED")
        self.assertEqual(execution["cost_scenario"], "BASE")
        self.assertEqual(self.dataset.public_manifest["eligibility_status"], "UNVERIFIED_HYPOTHETICAL_SESSION_SCENARIO")
        self.assertFalse(self.dataset.public_manifest["economics_eligible"])

    def test_scan_hash_or_evaluation_period_mismatch_blocks_backtest(self):
        bundle = self.dataset.scan_symbol("EURUSD")
        wrong_hash = {**bundle, "dataset_fingerprint": "f" * 64}
        with self.assertRaisesRegex(ValueError, "BACKTEST_SOURCE_MISMATCH"):
            self.dataset.assert_scan_parity(wrong_hash, symbol="EURUSD")
        wrong_period = {**bundle, "evaluation_end_utc": (DECISION + timedelta(days=1)).isoformat()}
        with self.assertRaisesRegex(ValueError, "BACKTEST_SOURCE_MISMATCH"):
            self.dataset.assert_scan_parity(wrong_period, symbol="EURUSD")
        with self.assertRaisesRegex(ValueError, "BACKTEST_SOURCE_MISMATCH"):
            validate_scan_dataset_fingerprint("a" * 64, "b" * 64)

    def test_frame_mutation_and_partial_bar_are_detected_or_excluded(self):
        original = self.dataset.public_manifest["bar_count_by_symbol_timeframe"]["EURUSD"]["M5"]
        self.assertEqual(self.dataset.public_manifest["partial_bars_excluded_by_symbol_timeframe"]["EURUSD"]["M5"], 1)
        self.dataset.frames_by_symbol["EURUSD"]["M5"].loc[0, "close"] += 0.01
        with self.assertRaisesRegex(ValueError, "FRAME_MUTATED"):
            self.dataset.scan_symbol("EURUSD")
        self.assertGreater(original, 692)


if __name__ == "__main__":
    unittest.main()
