import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reconcile_phase1_historical import (
    _base_cost_mismatches,
    _compare_hash_manifests,
    _compare_trade_rows,
    _evidence_has_provenance,
    _selection_witnesses_cover,
    _timestamp_within_window,
    _verify_dataset_bytes,
    verify_public_report,
    DATASET_SHA256,
    COMMITS,
    PUBLIC_OUTPUT,
)


def row(symbol="EURUSD", signal_time="2026-04-07T18:15:00+00:00", result="WIN", **extra):
    return {
        "symbol": symbol,
        "threshold": "65",
        "mode": "LIMIT",
        "signal_time": signal_time,
        "result": result,
        "entry": "1.1",
        "stop_loss": "1.0",
        "take_profit": "1.2",
        **extra,
    }


class HistoricalReconciliationTests(unittest.TestCase):
    def test_public_aggregate_report_uses_issue_required_name(self):
        self.assertEqual(PUBLIC_OUTPUT, "reports/phase1_final_local/baseline_aggregate_reconciliation.json")

    def test_hidden_price_change_invalidates_frozen_dataset(self):
        self.assertFalse(_verify_dataset_bytes(b"close=1.1000\n", b"close=1.1001\n"))
        content = b"frozen contents\n"
        self.assertFalse(_verify_dataset_bytes(content, content))  # Not the pinned dataset digest.

    def test_missing_operation_without_selection_witness_fails(self):
        result = _compare_trade_rows(
            [row(), row(symbol="USDCAD")],
            [row()],
            "pre_finalization_to_current",
            "portfolio",
            set(),
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["selection_witness_missing_count"], 1)

    def test_numeric_equality_without_private_provenance_stays_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "summary.json"
            self.assertEqual(verify_public_report(missing)["status"], "PENDING")
        self.assertFalse(_evidence_has_provenance({"numeric_deltas": []}))

    def test_manipulated_source_hash_is_rejected(self):
        self.assertFalse(_compare_hash_manifests({"source": {"a.py": "a" * 64}}, {"source": {"a.py": "b" * 64}}))
        self.assertTrue(_compare_hash_manifests({"source": {"a.py": "a" * 64}}, {"source": {"a.py": "a" * 64}}))

    def test_stale_reconciliation_report_is_not_reused(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.assertFalse(_timestamp_within_window(old))

    def test_selection_witnesses_must_exactly_cover_operation_delta(self):
        self.assertTrue(_selection_witnesses_cover({"old"}, {"new"}, {"old", "new"}))
        self.assertFalse(_selection_witnesses_cover({"old"}, {"new"}, {"old"}))

    def test_missing_or_nonzero_base_costs_are_not_waived(self):
        self.assertEqual(_base_cost_mismatches([row(cost_scenario="BASE", transaction_cost_eur="0")]), [])
        self.assertEqual(_base_cost_mismatches([row(cost_scenario="BASE", transaction_cost_eur="0.01")]), [0])
        self.assertEqual(_base_cost_mismatches([row(cost_scenario="STRESS", transaction_cost_eur="0")]), [0])

    def test_reconciliation_requires_pinned_dataset_and_commit_chain(self):
        bundle = {
            "dataset": {"sha256": DATASET_SHA256},
            "commits": dict(COMMITS),
            "replay_artifacts": {"a.csv": {"sha256": "a" * 64}},
            "source_hashes_from_git_objects": {"current": {"src.py": "b" * 64}},
        }
        self.assertTrue(_evidence_has_provenance(bundle))
        bundle["commits"] = {**COMMITS, "current": "0" * 40}
        self.assertFalse(_evidence_has_provenance(bundle))


if __name__ == "__main__":
    unittest.main()
