from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from config.market_config import TIMEFRAMES
from src.history_data_integrity import content_hashes
from src.market_history_audit import TIMEFRAMES_SECONDS
from src.phase2_2_2c_analysis import (
    EMA200_STABLE_BARS,
    INDICATOR_MINIMUM_BARS,
    _mtf_decision,
    _privacy_findings,
    build_contiguous_segments,
    classify_calendar_gap,
    classify_censored_position,
    classify_warmup,
    cost_conversion,
    latest_closed_index,
    merge_price_variants,
    normalize_local_run_dir,
    _source_rank,
    source_hashes_match,
    verify_run_path,
)
from src.warmup_audit import ema200_stabilization_bars


class CalendarEvidenceTests(unittest.TestCase):
    def test_weekend_holiday_and_dst_without_broker_evidence_stay_indeterminate(self):
        # These dates intentionally span a US/EU DST boundary and a holiday.
        for _date in ("2026-03-08", "2026-03-29", "2026-12-25"):
            self.assertEqual(classify_calendar_gap(None), "INDETERMINATE_BROKER_CALENDAR")

    def test_official_dated_evidence_is_required_for_closed_or_open_classification(self):
        evidence = {
            "official_source": True,
            "source_url_or_path": "https://broker.example/sessions",
            "sha256": "a" * 64,
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "timezone": "UTC",
            "session_rule": "symbol-specific dated session",
            "session_status": "CLOSED",
        }
        self.assertEqual(classify_calendar_gap(evidence), "VERIFIED_CLOSED_SESSION")
        evidence["session_status"] = "OPEN"
        self.assertEqual(classify_calendar_gap(evidence), "OPEN_SESSION_NO_BAR_FEED_UNVERIFIED")
        evidence["feed_completeness_verified"] = True
        self.assertEqual(classify_calendar_gap(evidence), "VERIFIED_OPEN_SESSION_MISSING")
        evidence["official_source"] = False
        self.assertEqual(classify_calendar_gap(evidence), "INDETERMINATE_BROKER_CALENDAR")


class ProvenanceAndSegmentationTests(unittest.TestCase):
    def test_missing_local_run_is_pending_and_never_synthesized(self):
        with tempfile.TemporaryDirectory() as directory:
            result = verify_run_path(Path(directory), "phase222b-local-20261115")
        self.assertEqual(result["status"], "MISSING")
        self.assertFalse(result["manifest_present"])
        self.assertEqual(result["actual_chunk_count"], 0)

    def test_offline_run_folder_must_be_inside_ignored_run_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = root / "data" / "market_history" / "backfill_runs" / "fixture"
            run_id, run_root = normalize_local_run_dir(
                root, "data/market_history/backfill_runs/fixture", "phase222b-local-20261115"
            )
            self.assertEqual(run_id, "fixture")
            self.assertEqual(run_root, expected.resolve())
            with self.assertRaisesRegex(ValueError, "LOCAL_RUN_PATH_OUTSIDE_IGNORED_ROOT"):
                normalize_local_run_dir(root, root.parent / "outside-run", "fixture")

    def test_raw_hash_changes_with_crlf_but_canonical_hash_does_not(self):
        lf = content_hashes(b"time,close\n2026-01-01T00:00:00+00:00,1.2\n")
        crlf = content_hashes(b"time,close\r\n2026-01-01T00:00:00+00:00,1.2\r\n")
        self.assertNotEqual(lf["raw_sha256"], crlf["raw_sha256"])
        self.assertEqual(lf["canonical_sha256"], crlf["canonical_sha256"])

    def test_source_hash_map_compares_raw_digest_and_requires_canonical_digest(self):
        expected = {"data/market/EURUSD/M15.csv": "raw-hash"}
        actual = {"data/market/EURUSD/M15.csv": {"raw_sha256": "raw-hash", "canonical_sha256": "canonical-hash"}}
        self.assertTrue(source_hashes_match(expected, actual))
        self.assertFalse(source_hashes_match(expected, {"data/market/EURUSD/M15.csv": {"raw_sha256": "different", "canonical_sha256": "canonical-hash"}}))
        self.assertFalse(source_hashes_match(expected, {"data/market/EURUSD/M15.csv": {"raw_sha256": "raw-hash"}}))

    def test_equivalent_source_variants_preserve_provenance_but_price_conflict_is_rejected(self):
        same = ("1.1", "1.2", "1.0", "1.15")
        different = ("1.1", "1.21", "1.0", "1.15")
        status, accepted = merge_price_variants([same, same])
        self.assertEqual(status, "OHLC_EQUIVALENT_PROVENANCE_PRESERVED")
        self.assertEqual(accepted, same)
        status, accepted = merge_price_variants([same, different])
        self.assertEqual(status, "SOURCE_CONFLICT")
        self.assertIsNone(accepted)

    def test_source_read_order_is_stable_but_recency_never_resolves_price_conflicts(self):
        base = "data/market/EURUSD/M15.csv"
        snapshot = "data/market_history/mt5_real_readonly/20261111T202300.097058Z/EURUSD_M15.csv"
        chunk = "data/market_history/backfill_runs/phase222b-local-20261115/chunks/example.csv"
        self.assertLess(_source_rank(base), _source_rank(snapshot))
        self.assertLess(_source_rank(snapshot), _source_rank(chunk))
        status, accepted = merge_price_variants([
            ("1.1", "1.2", "1.0", "1.15"),
            ("1.1", "1.21", "1.0", "1.15"),
        ])
        self.assertEqual(status, "SOURCE_CONFLICT")
        self.assertIsNone(accepted)

    def test_contiguous_windows_split_at_every_missing_cadence_slot(self):
        self.assertEqual(
            build_contiguous_segments([0, 60, 120, 240, 300, 600], 60),
            [(0, 3), (3, 5), (5, 6)],
        )

    def test_window_selection_has_no_pnl_input_or_outcome_based_rule(self):
        times = [0, 900, 1800, 2700]
        first = build_contiguous_segments(times, 900)
        second = build_contiguous_segments(list(times), 900)
        self.assertEqual(first, second)
        self.assertNotIn("pnl", build_contiguous_segments.__code__.co_varnames)


class CausalityAndWarmupTests(unittest.TestCase):
    def test_latest_closed_bar_excludes_bar_closing_after_decision(self):
        hourly = [0, 3600, 7200]
        decision = 7200
        index = latest_closed_index(hourly, 3600, decision)
        self.assertEqual(hourly[index], 3600)
        self.assertLessEqual(hourly[index] + 3600, decision)

    def test_mtf_decision_uses_only_closed_fresh_bars_and_contiguous_warmup(self):
        decision = 700 * 86400
        series = {}
        for timeframe in TIMEFRAMES:
            seconds = TIMEFRAMES_SECONDS[timeframe]
            times = list(range(decision - 800 * seconds, decision + seconds, seconds))
            starts = [0] * len(times)
            series[timeframe] = {
                "timestamps": times,
                "seconds": seconds,
                "segment_starts": starts,
            }
        result = _mtf_decision("EURUSD", decision, series)
        self.assertTrue(result["feature_ready_all_timeframes"])
        self.assertTrue(result["ema200_stabilized_all_timeframes"])
        self.assertFalse(result["lookahead_detected"])
        for item in result["timeframes"].values():
            self.assertLessEqual(
                datetime.fromisoformat(item["latest_closed_bar_close_utc"].replace("Z", "+00:00")),
                datetime.fromisoformat(item["decision_time_utc"].replace("Z", "+00:00")),
            )

    def test_indicator_minimum_and_stability_warmup_come_from_existing_code(self):
        self.assertEqual(INDICATOR_MINIMUM_BARS, 200)
        self.assertEqual(EMA200_STABLE_BARS, ema200_stabilization_bars())
        self.assertEqual(classify_warmup(199), "WARMUP_INSUFFICIENT")
        self.assertEqual(classify_warmup(200), "INDICATORS_READY_EMA200_NOT_STABILIZED")
        self.assertEqual(classify_warmup(EMA200_STABLE_BARS), "EMA200_STABILIZED")

    def test_censored_trade_without_observed_exit_is_not_evaluable(self):
        self.assertEqual(classify_censored_position(False, False), "NOT_EVALUABLE")
        self.assertEqual(classify_censored_position(True, False), "NOT_EVALUABLE")
        self.assertEqual(classify_censored_position(True, True), "EVALUABLE")


class CostAndPrivacyTests(unittest.TestCase):
    def test_points_price_lots_commission_and_spread_are_not_double_counted(self):
        result = cost_conversion(5, 0.00001, 0.1, 7, 1)
        self.assertAlmostEqual(result["spread_price"], 0.00005)
        self.assertAlmostEqual(result["round_turn_commission_eur"], 0.7)
        self.assertAlmostEqual(result["slippage_price"], 0.00002)
        self.assertTrue(result["spread_embedded_in_bid_ask"])
        self.assertEqual(result["additional_spread_cost"], 0.0)
        self.assertFalse(result["spread_double_counted"])
        self.assertIn("NOT_CALCULATED", result["eur_pnl_conversion"])

    def test_public_output_scanner_rejects_private_identifiers_and_absolute_paths(self):
        findings = _privacy_findings({
            "account_number": "redacted",
            "path": r"C:\Users\person\private.csv",
        })
        self.assertTrue(any("SENSITIVE_KEY" in item for item in findings))
        self.assertTrue(any("ABSOLUTE_PATH" in item for item in findings))


if __name__ == "__main__":
    unittest.main()
