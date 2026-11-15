from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from src.phase2_2_2d_sessions import (
    TIMEFRAME_SECONDS,
    build_session_aware_segments,
    classify_missing_bar_slot,
    evaluate_broker_session_mtf_decision,
    expected_bar_close_utc,
    validate_session_manifest,
)


UTC = timezone.utc


def manifest(*, timezone_rule=None, boundary_policy=None):
    weekdays = {
        str(day): ([{"start_local": "00:00", "end_local": "24:00"}] if day < 5 else [])
        for day in range(7)
    }
    return {
        "schema_version": 1,
        "provider_id": "SYNTHETIC_TEST_ONLY",
        "evidence": {
            "status": "OFFICIAL_BROKER_DOCUMENTED",
            "official_source": True,
            "source_url": "https://broker.example.test/sessions.pdf",
            "sha256": "a" * 64,
            "accessed_utc": "2026-01-01T00:00:00Z",
            "effective_from_utc": "2020-01-01T00:00:00Z",
            "effective_to_utc": "2030-01-01T00:00:00Z",
        },
        "symbols": ["EURUSD", "USDCAD", "AUDUSD"],
        "timeframes": list(TIMEFRAME_SECONDS),
        "timezone_rule": timezone_rule or {"mode": "IANA", "name": "UTC"},
        "sessions_complete": True,
        "exceptions_complete": True,
        "exception_coverage": {"from_local_date": "2020-01-01", "through_local_date": "2030-01-01"},
        "weekly_sessions": weekdays,
        "exceptions": {},
        "timeframe_boundary_policy": boundary_policy or {
            timeframe: "FIXED_UTC" for timeframe in TIMEFRAME_SECONDS
        },
    }


class BrokerSessionEvidenceTests(unittest.TestCase):
    def test_missing_manifest_or_unverified_quote_never_proves_closed(self):
        opened = "2025-01-04T12:00:00Z"
        self.assertEqual(classify_missing_bar_slot(None, "EURUSD", "M15", opened), "UNKNOWN")
        self.assertEqual(
            classify_missing_bar_slot(None, "EURUSD", "M15", opened, observed_no_quote=True),
            "OBSERVED_NO_QUOTE_UNCONFIRMED",
        )

    def test_manifest_requires_official_source_hash_dates_and_symbol_scope(self):
        good = manifest()
        at = "2025-01-06T12:00:00Z"
        self.assertIsNone(validate_session_manifest(good, "EURUSD", "M15", at))
        for change, expected in (
            ({"evidence": {**good["evidence"], "official_source": False}}, "SESSION_OFFICIAL_SOURCE_UNVERIFIED"),
            ({"evidence": {**good["evidence"], "sha256": "bad"}}, "SESSION_SOURCE_HASH_INVALID"),
            ({"evidence": {**good["evidence"], "effective_from_utc": None}}, "SESSION_EFFECTIVE_START_MISSING"),
            ({"symbols": ["USDCAD"]}, "SESSION_SCOPE_MISMATCH"),
        ):
            candidate = {**good, **change}
            self.assertEqual(validate_session_manifest(candidate, "EURUSD", "M15", at), expected)

    def test_effective_date_and_exception_coverage_fail_closed(self):
        candidate = manifest()
        candidate["evidence"] = {**candidate["evidence"], "effective_to_utc": "2025-01-01T00:00:00Z"}
        self.assertEqual(
            classify_missing_bar_slot(candidate, "EURUSD", "M15", "2025-01-06T12:00:00Z"),
            "UNKNOWN",
        )
        candidate = manifest()
        candidate["exception_coverage"] = {"from_local_date": "2020-01-01", "through_local_date": "2024-12-31"}
        self.assertEqual(
            classify_missing_bar_slot(candidate, "EURUSD", "M15", "2025-01-06T12:00:00Z"),
            "UNKNOWN",
        )

    def test_verified_open_missing_requires_feed_completeness(self):
        evidence = manifest()
        opened = "2025-01-06T12:00:00Z"
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", opened),
            "BROKER_SESSION_OPEN_FEED_UNVERIFIED",
        )
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", opened, feed_completeness_verified=True),
            "VERIFIED_BROKER_OPEN_NO_BAR",
        )

    def test_verified_closed_and_explicit_holiday_exception(self):
        evidence = manifest()
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2025-01-04T12:00:00Z"),
            "VERIFIED_BROKER_CLOSED",
        )
        evidence["exceptions"]["2025-01-06"] = []
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2025-01-06T12:00:00Z"),
            "VERIFIED_BROKER_CLOSED",
        )
        evidence["exceptions"]["2025-12-25"] = []
        evidence["exceptions"]["2026-01-01"] = []
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2025-12-25T12:00:00Z"),
            "VERIFIED_BROKER_CLOSED",
        )
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2026-01-01T12:00:00Z"),
            "VERIFIED_BROKER_CLOSED",
        )

    def test_overlapping_session_windows_and_fixed_utc_misalignment_fail_closed(self):
        evidence = manifest()
        evidence["weekly_sessions"]["0"] = [
            {"start_local": "00:00", "end_local": "12:00"},
            {"start_local": "11:30", "end_local": "14:00"},
        ]
        self.assertEqual(
            validate_session_manifest(evidence, "EURUSD", "M15", "2025-01-06T12:00:00Z"),
            "SESSION_WINDOWS_OVERLAP",
        )
        evidence = manifest()
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2025-01-06T00:01:00Z"),
            "UNKNOWN",
        )
        with self.assertRaisesRegex(ValueError, "FIXED_UTC_BAR_ALIGNMENT"):
            expected_bar_close_utc("2025-01-06T00:01:00Z", "M15", evidence)

    def test_unknown_boundary_and_partial_session_overlap_are_not_closed(self):
        evidence = manifest()
        evidence["timeframe_boundary_policy"].pop("M15")
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2025-01-06T12:00:00Z"),
            "UNKNOWN",
        )
        evidence = manifest()
        evidence["weekly_sessions"]["0"] = [{"start_local": "00:01", "end_local": "23:59"}]
        self.assertEqual(
            classify_missing_bar_slot(evidence, "EURUSD", "M15", "2025-01-06T00:00:00Z"),
            "UNKNOWN",
        )

    def test_strict_mode_preserves_fixed_cadence_baseline(self):
        start = int(datetime(2025, 1, 6, tzinfo=UTC).timestamp())
        stamps = [start, start + 900, start + 3 * 900]
        self.assertEqual(
            build_session_aware_segments(stamps, "M15", "EURUSD", None),
            [(0, 2), (2, 3)],
        )

    def test_verified_weekend_closure_bridges_but_intraday_open_gap_splits(self):
        evidence = manifest()
        friday = int(datetime(2025, 1, 3, 23, 45, tzinfo=UTC).timestamp())
        monday = int(datetime(2025, 1, 6, 0, 0, tzinfo=UTC).timestamp())
        self.assertEqual(
            build_session_aware_segments([friday, monday], "M15", "EURUSD", evidence),
            [(0, 2)],
        )
        monday_open = int(datetime(2025, 1, 6, 10, 0, tzinfo=UTC).timestamp())
        thirty_minutes_later = monday_open + 2 * 900
        self.assertEqual(
            build_session_aware_segments([monday_open, thirty_minutes_later], "M15", "EURUSD", evidence),
            [(0, 1), (1, 2)],
        )

    def test_timezone_ambiguous_and_nonexistent_local_times_are_rejected(self):
        from src.phase2_2_2d_sessions import _local_to_utc, _timezone_rule

        evidence = manifest(timezone_rule={"mode": "IANA", "name": "America/New_York"})
        rule = _timezone_rule(evidence)
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS"):
            _local_to_utc(datetime(2025, 11, 2, 1, 30), rule)
        with self.assertRaisesRegex(ValueError, "NONEXISTENT"):
            _local_to_utc(datetime(2025, 3, 9, 2, 30), rule)

    def test_new_york_server_offset_maps_dst_and_d1_transition_duration(self):
        evidence = manifest(
            timezone_rule={
                "mode": "NEW_YORK_SERVER_OFFSET",
                "reference_timezone": "America/New_York",
                "standard_offset_minutes": 120,
                "daylight_offset_minutes": 180,
            },
            boundary_policy={**{tf: "FIXED_UTC" for tf in TIMEFRAME_SECONDS}, "D1": "SERVER_LOCAL_DAILY"},
        )
        from src.phase2_2_2d_sessions import _local_to_utc, _server_local, _timezone_rule

        rule = _timezone_rule(evidence)
        self.assertEqual(_server_local("2025-01-15T12:00:00Z", rule), datetime(2025, 1, 15, 14, 0))
        self.assertEqual(_server_local("2025-06-15T12:00:00Z", rule), datetime(2025, 6, 15, 15, 0))
        march_open = _local_to_utc(datetime(2025, 3, 9, 0, 0), rule)
        march_close = expected_bar_close_utc(march_open, "D1", evidence)
        self.assertEqual((march_close - march_open).total_seconds(), 23 * 3600)
        november_open = _local_to_utc(datetime(2025, 11, 2, 0, 0), rule)
        november_close = expected_bar_close_utc(november_open, "D1", evidence)
        self.assertEqual((november_close - november_open).total_seconds(), 25 * 3600)

        evidence["timeframe_boundary_policy"]["H4"] = "SERVER_WALL_CLOCK"
        h4_open = _local_to_utc(datetime(2025, 3, 9, 6, 0), rule)
        h4_close = expected_bar_close_utc(h4_open, "H4", evidence)
        self.assertEqual((h4_close - h4_open).total_seconds(), 3 * 3600)

    def test_partial_bar_is_excluded_at_decision_boundary(self):
        evidence = manifest()
        evidence["weekly_sessions"] = {
            str(day): [{"start_local": "00:00", "end_local": "24:00"}]
            for day in range(7)
        }
        decision = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        count = 200
        series = {
            timeframe: [
                int((decision - timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * offset)).timestamp())
                for offset in range(count + 1, 0, -1)
            ]
            for timeframe in TIMEFRAME_SECONDS
        }
        result = evaluate_broker_session_mtf_decision("EURUSD", decision - timedelta(seconds=1), series, evidence)
        self.assertFalse(result["lookahead_detected"])
        for timeframe, details in result["timeframes"].items():
            self.assertLessEqual(
                datetime.fromisoformat(details["latest_closed_bar_close_utc"].replace("Z", "+00:00")),
                decision - timedelta(seconds=1),
                timeframe,
            )

    def test_d1_policy_rejects_non_midnight_open(self):
        evidence = manifest(boundary_policy={**{tf: "FIXED_UTC" for tf in TIMEFRAME_SECONDS}, "D1": "SERVER_LOCAL_DAILY"})
        with self.assertRaisesRegex(ValueError, "D1_BOUNDARY_MISMATCH"):
            expected_bar_close_utc("2025-01-06T01:00:00Z", "D1", evidence)

    def test_mtf_warmup_thresholds_199_200_691_692_and_no_lookahead(self):
        evidence = manifest()
        evidence["weekly_sessions"] = {
            str(day): [{"start_local": "00:00", "end_local": "24:00"}]
            for day in range(7)
        }
        decision = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

        def build_series(count):
            return {
                timeframe: [
                    int((decision - timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * offset)).timestamp())
                    for offset in range(count, 0, -1)
                ]
                for timeframe in TIMEFRAME_SECONDS
            }

        for count, expected_ready, expected_stable in (
            (199, False, False), (200, True, False), (691, True, False), (692, True, True),
        ):
            result = evaluate_broker_session_mtf_decision("EURUSD", decision, build_series(count), evidence)
            self.assertEqual(result["feature_ready_all_timeframes"], expected_ready, count)
            self.assertEqual(result["ema200_stabilized_all_timeframes"], expected_stable, count)
            self.assertFalse(result["lookahead_detected"])

    def test_missing_calendar_keeps_mtf_ineligible_even_with_many_bars(self):
        decision = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        series = {
            timeframe: [
                int((decision - timedelta(seconds=TIMEFRAME_SECONDS[timeframe] * offset)).timestamp())
                for offset in range(692, 0, -1)
            ]
            for timeframe in TIMEFRAME_SECONDS
        }
        result = evaluate_broker_session_mtf_decision("EURUSD", decision, series, None)
        self.assertFalse(result["calendar_verified"])
        self.assertFalse(result["feature_ready_all_timeframes"])
        self.assertIn("BROKER_CALENDAR_UNVERIFIED", result["ineligibility_reasons"])


if __name__ == "__main__":
    unittest.main()
