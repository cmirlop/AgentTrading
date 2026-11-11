from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from phase2_2_2a_scenarios import (
    AS_OF,
    SYNTHETIC_SYMBOLS,
    SYNTHETIC_TIMEFRAMES,
    SyntheticMT5,
    _audit_run,
    create_synthetic_source_tree,
    run_end_to_end_scenarios,
)
from src.market_history_audit import audit_market_history
from src.market_history_backfill import execute_backfill_plan


class EndToEndBackfillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = run_end_to_end_scenarios()

    def test_clean_interrupted_resume_reaudit_verifies_continuity(self) -> None:
        result = self.scenarios
        clean = result["clean_recovery"]
        resume = result["interruption_and_resume"]
        self.assertTrue(resume["interruption_observed"])
        self.assertEqual(resume["first_chunk_files_after_interruption"], 1)
        self.assertTrue(resume["first_completed_chunk_not_refetched"])
        self.assertEqual(clean["backfill_integrity_status"], "PASS")
        self.assertEqual(clean["continuity_status"], "HISTORY_CONTINUITY_VERIFIED")
        self.assertEqual(clean["remaining_recovery_intervals"], 0)
        self.assertEqual(clean["calendar_review_intervals"], 0)
        self.assertTrue(clean["originals_unchanged"])
        self.assertEqual(result["post_backfill_audit"]["data_provenance"], "SYNTHETIC_TEST_DATA_NOT_REAL_RECOVERY")

    def test_post_audit_exposes_raw_and_canonical_hash_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_source_tree(root)
            _, _, plan = audit_market_history(
                root,
                as_of_utc=AS_OF,
                symbols=SYNTHETIC_SYMBOLS,
                timeframes_seconds=SYNTHETIC_TIMEFRAMES,
            )
            execute_backfill_plan(SyntheticMT5(), plan, root=root, run_id="hash-evidence", as_of_utc=AS_OF)
            report = _audit_run(root, "hash-evidence")
        self.assertEqual(report["history_continuity_status"], "HISTORY_CONTINUITY_VERIFIED")
        accepted = [item for item in report["backfill_run"]["chunks"] if item.get("accepted")]
        self.assertTrue(accepted)
        for chunk in accepted:
            self.assertEqual(chunk["raw_sha256_expected"], chunk["raw_sha256_actual"])
            self.assertEqual(chunk["canonical_sha256_expected"], chunk["canonical_sha256_actual"])
            self.assertEqual(chunk["canonicalization_policy"], "CSV_LINE_ENDINGS_LF_V1")

    def test_unexpected_source_file_blocks_continuity_even_with_valid_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_source_tree(root)
            _, _, plan = audit_market_history(
                root,
                as_of_utc=AS_OF,
                symbols=SYNTHETIC_SYMBOLS,
                timeframes_seconds=SYNTHETIC_TIMEFRAMES,
            )
            execute_backfill_plan(SyntheticMT5(), plan, root=root, run_id="extra-source", as_of_utc=AS_OF)
            (root / "data/market_history/mt5_real_readonly/20261109T202112.171333Z/EXTRA.csv").write_text("untracked\n", encoding="utf-8")
            report = _audit_run(root, "extra-source")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["history_continuity_status"], "HISTORY_CONTINUITY_BLOCKED")
        self.assertIn("SNAPSHOT_SOURCE_FILES_UNEXPECTED", report["critical_integrity_errors"])

    def test_price_conflict_is_retained_and_blocks_continuity(self) -> None:
        conflict = self.scenarios["conflict_recovery"]
        self.assertEqual(conflict["continuity_status"], "HISTORY_CONTINUITY_BLOCKED")
        self.assertEqual(conflict["conflict_status"], "BACKFILL_SOURCE_CONFLICT_REQUIRES_REVIEW")
        self.assertGreater(conflict["conflict_count"], 0)
        self.assertEqual(conflict["conflict_action"], "BACKFILL_SOURCE_CONFLICT_REQUIRES_REVIEW")

    def test_empty_response_remains_pending_and_continuity_is_partial(self) -> None:
        empty = self.scenarios["empty_response_recovery"]
        self.assertEqual(empty["continuity_status"], "HISTORY_CONTINUITY_PARTIAL")
        self.assertGreater(empty["empty_chunks"], 0)
        self.assertGreater(empty["pending_chunk_count"], 0)
        self.assertTrue(self.scenarios["original_sources_unchanged_after_all_scenarios"])

    def test_corrupt_chunk_hash_blocks_post_backfill_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_source_tree(root)
            _, _, plan = audit_market_history(
                root,
                as_of_utc=AS_OF,
                symbols=SYNTHETIC_SYMBOLS,
                timeframes_seconds=SYNTHETIC_TIMEFRAMES,
            )
            execute_backfill_plan(SyntheticMT5(), plan, root=root, run_id="corrupt-test", as_of_utc=AS_OF)
            chunk = next((root / "data/market_history/backfill_runs/corrupt-test/chunks").glob("*.csv"))
            lines = chunk.read_bytes().splitlines()
            fields = lines[1].split(b",")
            fields[1] = b"1.999"
            lines[1] = b",".join(fields)
            chunk.write_bytes(b"\n".join(lines) + b"\n")
            report = _audit_run(root, "corrupt-test")
        self.assertEqual(report["history_continuity_status"], "HISTORY_CONTINUITY_BLOCKED")
        self.assertIn("BACKFILL_CHUNK_HASH_MISMATCH", report["backfill_run"]["errors"])

    def test_resume_with_changed_plan_is_rejected_without_market_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_source_tree(root)
            _, _, plan = audit_market_history(
                root,
                as_of_utc=AS_OF,
                symbols=SYNTHETIC_SYMBOLS,
                timeframes_seconds=SYNTHETIC_TIMEFRAMES,
            )
            execute_backfill_plan(SyntheticMT5(), plan, root=root, run_id="plan-change", as_of_utc=AS_OF)
            altered = json.loads(json.dumps(plan))
            altered["chunk_policy"]["overlap_bars_each_side"] = 1
            api = SyntheticMT5()
            with self.assertRaisesRegex(RuntimeError, "BACKFILL_RESUME_PROVENANCE_MISMATCH"):
                execute_backfill_plan(api, altered, root=root, run_id="plan-change", as_of_utc=AS_OF)
        self.assertEqual(api.copy_count, 0)

    def test_orphan_chunk_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_source_tree(root)
            _, _, plan = audit_market_history(
                root,
                as_of_utc=AS_OF,
                symbols=SYNTHETIC_SYMBOLS,
                timeframes_seconds=SYNTHETIC_TIMEFRAMES,
            )
            execute_backfill_plan(SyntheticMT5(), plan, root=root, run_id="orphan-test", as_of_utc=AS_OF)
            chunks = root / "data/market_history/backfill_runs/orphan-test/chunks"
            (chunks / "unplanned.csv").write_text("orphan\n", encoding="utf-8")
            report = _audit_run(root, "orphan-test")
        self.assertEqual(report["history_continuity_status"], "HISTORY_CONTINUITY_BLOCKED")
        self.assertIn("BACKFILL_ORPHAN_CHUNK_FILE", report["backfill_run"]["errors"])

    def test_old_manifest_schema_is_rejected_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_synthetic_source_tree(root)
            run = root / "data/market_history/backfill_runs/old-run"
            run.mkdir(parents=True)
            (run / "manifest.json").write_text(json.dumps({"schema_version": 1, "run_id": "old-run"}), encoding="utf-8")
            report = _audit_run(root, "old-run")
        self.assertEqual(report["history_continuity_status"], "HISTORY_CONTINUITY_BLOCKED")
        self.assertIn("BACKFILL_MANIFEST_SCHEMA_UNSUPPORTED", report["backfill_run"]["errors"])


if __name__ == "__main__":
    unittest.main()
