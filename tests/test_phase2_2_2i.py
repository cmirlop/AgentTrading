from __future__ import annotations

import json
from pathlib import Path
import unittest

from src.market_gap_classification import (
    EXPECTED_GAP_COUNT,
    build_classification_summary,
    build_episode_groups,
    annotate_episode_alignment,
    classify_gap,
    extract_gap_inventory,
    official_dst_transition_dates,
    source_registry,
    validate_gap_record,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "reports/phase2_2_2h/public_2023_source_manifest.json"


def _gap(previous: str, following: str, nominal: int, symbol: str = "EURUSD", timeframe: str = "M5"):
    from datetime import datetime, timedelta
    prev = datetime.fromisoformat(previous)
    nxt = datetime.fromisoformat(following)
    start = prev + timedelta(seconds=nominal)
    return {
        "gap_id": f"{symbol}-{timeframe}-{previous}",
        "symbol": symbol,
        "timeframe": timeframe,
        "source_path": f"data/market/{symbol}/{timeframe}.csv",
        "source_raw_sha256": "a" * 64,
        "source_canonical_sha256": "b" * 64,
        "previous_open_utc": prev.isoformat(),
        "previous_bar_open_utc": prev.isoformat(),
        "next_open_utc": nxt.isoformat(),
        "next_bar_open_utc": nxt.isoformat(),
        "previous_open_weekday_utc": prev.strftime("%A"),
        "next_open_weekday_utc": nxt.strftime("%A"),
        "gap_start_utc": start.isoformat(),
        "gap_end_utc": nxt.isoformat(),
        "gap_seconds": int((nxt - start).total_seconds()),
        "elapsed_seconds": int((nxt - prev).total_seconds()),
        "observed_open_delta_seconds": int((nxt - prev).total_seconds()),
        "nominal_cadence_seconds": nominal,
        "nominal_timeframe_seconds": nominal,
        "as_of_utc": "2026-11-17T20:13:49.261352+00:00",
        "episode_id": "",
    }


class Phase222iTests(unittest.TestCase):
    def setUp(self):
        self.sources = {
            "public_data": "PUBLIC_DATA_2023_PLUS",
            "bis_fx_context": "BIS_FX_GLOBAL_CONTEXT",
            "uk_holidays": "UK_GOV_HOLIDAYS",
            "us_holidays": "US_OPM_HOLIDAYS",
            "us_dst": "US_NIST_DST",
            "eu_dst": "EU_SUMMERTIME_DATES",
        }
        self.dst_dates = official_dst_transition_dates()

    def test_weekend_pattern_is_supported_but_never_verified_broker_closure(self):
        result = classify_gap(_gap("2026-01-09T23:55:00+00:00", "2026-01-12T00:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        self.assertEqual(result["classification"], "SUPPORTED_WEEKEND_PATTERN_BROKER_UNVERIFIED")
        self.assertFalse(result["verified_broker_closure"])
        self.assertEqual(result["broker_entity_status"], "BROKER_ENTITY_UNVERIFIED")
        self.assertGreater(result["remaining_unknown_portion"]["seconds"], 0)

    def test_public_holiday_overlap_is_context_only(self):
        result = classify_gap(_gap("2025-12-24T23:55:00+00:00", "2025-12-26T00:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        self.assertEqual(result["classification"], "SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED")
        self.assertEqual(result["holiday_context_utc_dates"], ["2025-12-25"])
        self.assertFalse(result["verified_broker_closure"])

    def test_historical_observed_and_country_holidays_are_context_only(self):
        new_year_observed = classify_gap(_gap("2023-01-02T01:40:00+00:00", "2023-01-02T01:55:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        labor_day = classify_gap(_gap("2024-09-02T00:40:00+00:00", "2024-09-02T00:50:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        canada_day = classify_gap(_gap("2026-07-01T00:30:00+00:00", "2026-07-01T00:40:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        for item in (new_year_observed, labor_day, canada_day):
            self.assertEqual(item["classification"], "SUPPORTED_HOLIDAY_PATTERN_BROKER_UNVERIFIED")
            self.assertFalse(item["verified_cause"])
            self.assertGreater(item["remaining_unknown_portion"]["seconds"], 0)

    def test_weekday_intraday_gap_is_not_called_a_session_close(self):
        result = classify_gap(_gap("2023-08-24T20:00:00+00:00", "2023-08-24T21:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        self.assertEqual(result["classification"], "UNRESOLVED_OPEN_SESSION_GAP")
        self.assertIn("feed-completeness", result["reason"])

    def test_nonstandard_multiday_gap_remains_unresolved(self):
        result = classify_gap(_gap("2023-03-06T12:00:00+00:00", "2023-03-09T12:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        self.assertEqual(result["classification"], "UNRESOLVED")
        self.assertFalse(result["verified_broker_closure"])

    def test_dst_proximity_is_context_not_attribution(self):
        result = classify_gap(_gap("2024-03-08T23:55:00+00:00", "2024-03-11T00:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        self.assertIn("2024-03-10", result["dst_context_utc_dates"])
        self.assertIn("timezone is unknown", " ".join(result["notes"]))
        self.assertFalse(result["verified_broker_closure"])

    def test_same_missing_window_forms_descriptive_episode_only(self):
        first = classify_gap(_gap("2026-01-09T23:55:00+00:00", "2026-01-12T00:00:00+00:00", 300, "EURUSD", "M5"), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        second = classify_gap(_gap("2026-01-09T23:45:00+00:00", "2026-01-12T00:00:00+00:00", 900, "USDCAD", "M15"), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        groups = build_episode_groups([first, second])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["observed_gap_count"], 2)
        self.assertIn("not proof of shared cause", groups[0]["grouping_semantics"])

    def test_weekly_episode_with_edge_difference_keeps_the_tail_unverified(self):
        first = classify_gap(_gap("2023-06-16T23:55:00+00:00", "2023-06-19T00:00:00+00:00", 300, "EURUSD", "M5"), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        second = classify_gap(_gap("2023-06-16T23:55:00+00:00", "2023-06-19T00:05:00+00:00", 300, "USDCAD", "M5"), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        rows = [first, second]
        annotate_episode_alignment(rows)
        self.assertEqual(rows[1]["episode_alignment_status"], "EDGE_WINDOW_DIFFERENCE_REQUIRES_REVIEW")
        self.assertGreater(rows[1]["remaining_unknown_portion"]["seconds"], 0)
        self.assertFalse(rows[1]["verified_cause"])

    def test_summary_has_required_dimensions_and_no_verified_causes(self):
        rows = [
            classify_gap(_gap("2026-01-09T23:55:00+00:00", "2026-01-12T00:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates),
            classify_gap(_gap("2023-08-24T20:00:00+00:00", "2023-08-24T21:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates),
        ]
        summary = build_classification_summary(rows)
        for key in ("by_symbol", "by_timeframe", "by_symbol_timeframe", "by_year", "by_month", "by_type", "by_reason", "by_provenance", "by_confidence"):
            self.assertIn(key, summary)
        self.assertEqual(summary["verified_broker_closure_count"], 0)

    def test_source_registry_never_trusts_context_as_broker_calendar(self):
        registry = source_registry("2026-11-17T20:13:49.261352+00:00", "c" * 64)
        self.assertEqual(registry["trusted_broker_calendar_hosts"], [])
        self.assertTrue(all(not source["trusted_for_broker_sessions"] for source in registry["sources"]))

    def test_schema_rejects_verified_or_mixed_without_exact_evidence(self):
        row = classify_gap(_gap("2026-01-09T23:55:00+00:00", "2026-01-12T00:00:00+00:00", 300), source_ids=self.sources, dst_transition_dates=self.dst_dates)
        forged = dict(row, classification="VERIFIED_BROKER_CLOSED")
        with self.assertRaisesRegex(ValueError, "VERIFIED_GAP_WITHOUT_PROVIDER_EVIDENCE"):
            validate_gap_record(forged)
        partial = dict(row, classification="MIXED_PARTIAL_EVIDENCE")
        with self.assertRaisesRegex(ValueError, "MIXED_GAP_WITHOUT_EXPLICIT_PARTIAL_COVERAGE"):
            validate_gap_record(partial)

    def test_real_snapshot_has_exact_3167_stable_gap_ids_and_no_verified_closures(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        first_pass = extract_gap_inventory(ROOT, manifest)
        second_pass = extract_gap_inventory(ROOT, manifest)
        self.assertEqual(len(first_pass), EXPECTED_GAP_COUNT)
        self.assertEqual([item["gap_id"] for item in first_pass], [item["gap_id"] for item in second_pass])
        self.assertEqual(len({item["gap_id"] for item in first_pass}), EXPECTED_GAP_COUNT)
        classified = [classify_gap(item, source_ids=self.sources, dst_transition_dates=self.dst_dates) for item in first_pass]
        self.assertEqual(len(classified), EXPECTED_GAP_COUNT)
        self.assertFalse(any(item["classification"].startswith("VERIFIED_") for item in classified))
        self.assertFalse(any(item["verified_broker_closure"] for item in classified))
        self.assertTrue(all("previous_bar_open_utc" in item and "elapsed_seconds" in item and "reason_code" in item for item in classified))
        registry = source_registry("2026-11-17T20:13:49.261352+00:00", "c" * 64)
        known_evidence = {item["evidence_id"] for item in registry["sources"]}
        self.assertTrue(all(set(item["evidence_ids"]) <= known_evidence for item in classified))
        summary = build_classification_summary(classified)
        self.assertEqual(sum(summary["classification_counts"].values()), EXPECTED_GAP_COUNT)
        self.assertEqual(summary["outcome_rollup"]["verified"], 0)
        self.assertEqual(summary["outcome_rollup"]["mixed"], 0)


if __name__ == "__main__":
    unittest.main()
