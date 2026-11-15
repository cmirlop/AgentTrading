from __future__ import annotations

import unittest

from scripts.finalize_phase2_2_2b import (
    _privacy_scan,
    derive_phase_status,
    reconcile_missing_cadence_slots,
)


class FinalPhaseStatusTests(unittest.TestCase):
    def test_pending_history_gaps_are_complete_with_pending_not_verified(self) -> None:
        checks = {
            "offline": True,
            "read_only": True,
            "integrity": True,
        }
        status, blockers = derive_phase_status(
            checks,
            continuity_status="HISTORY_CONTINUITY_PARTIAL",
            pending_recovery_intervals=74,
            calendar_review_intervals=1776,
        )
        self.assertEqual(status, "PHASE2_2_2B_COMPLETE_WITH_PENDING_GAPS")
        self.assertEqual(blockers, [])

    def test_failed_safety_gate_blocks_even_when_continuity_is_verified(self) -> None:
        status, blockers = derive_phase_status(
            {"offline": True, "tradeapi_disabled": False},
            continuity_status="HISTORY_CONTINUITY_VERIFIED",
            pending_recovery_intervals=0,
            calendar_review_intervals=0,
        )
        self.assertEqual(status, "PHASE2_2_2B_BLOCKED")
        self.assertEqual(blockers, ["tradeapi_disabled"])

    def test_data_verified_requires_no_gaps_or_calendar_review(self) -> None:
        status, blockers = derive_phase_status(
            {"integrity": True},
            continuity_status="HISTORY_CONTINUITY_VERIFIED",
            pending_recovery_intervals=0,
            calendar_review_intervals=0,
        )
        self.assertEqual(status, "PHASE2_2_2B_COMPLETE_DATA_VERIFIED")
        self.assertEqual(blockers, [])

    def test_public_report_privacy_scan_rejects_sensitive_fields_and_absolute_paths(self) -> None:
        self.assertEqual(_privacy_scan({"account_type": "REAL", "orders_sent": 0}), [])
        findings = _privacy_scan({
            "account_number": "redacted",
            "evidence": r"C:\Users\private\report.json",
        })
        self.assertEqual(len(findings), 2)

    def test_gap_slot_reconciliation_counts_removed_positions_without_calling_them_tradable(self) -> None:
        initial = [{
            "symbol": "EURUSD",
            "timeframe": "M5",
            "missing_start_utc": "2026-01-01T00:00:00+00:00",
            "missing_end_utc": "2026-01-01T00:10:00+00:00",
        }]
        post = [{
            "symbol": "EURUSD",
            "timeframe": "M5",
            "missing_start_utc": "2026-01-01T00:05:00+00:00",
            "missing_end_utc": "2026-01-01T00:10:00+00:00",
        }]
        result = reconcile_missing_cadence_slots(initial, post)
        self.assertEqual(result["initial_reviewed_missing_cadence_slots"], 3)
        self.assertEqual(result["post_reviewed_missing_cadence_slots"], 2)
        self.assertEqual(result["missing_cadence_slots_removed"], 1)
        self.assertEqual(result["missing_cadence_slots_added"], 0)
        self.assertIn("not a count", result["interpretation"])


if __name__ == "__main__":
    unittest.main()
