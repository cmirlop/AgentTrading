from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from datetime import timedelta
import unittest
from unittest.mock import patch

from config.market_config import SYMBOLS, TIMEFRAMES
from src.market_history_audit import REQUIRED_COLUMNS
from src.public_2023_history_audit import (
    audit_public_2023_history,
    build_public_2023_source_manifest,
    load_public_2023_verified_dataset,
    validate_public_2023_source_manifest,
)
from src.historical_execution import simulate_trade
from src.historical_state import build_historical_market_state
from src.historical_trade_planner import build_historical_trade_plan
from src.verified_research_dataset import VerifiedResearchDataset
from tests.test_verified_dataset_backtests import (
    synthetic_frames,
    synthetic_session_manifest,
)
import pandas as pd


BASE_SHA = "a" * 40
AS_OF = "2023-01-05T00:00:00+00:00"
TIMES = ("2023-01-02T00:00:00+00:00", "2023-01-03T00:00:00+00:00")


class Public2023HistoryAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        scan = b"synthetic frozen scan\n"
        scan_path = self.root / "data/backtest/technical_candidate_scan.csv"
        scan_path.parent.mkdir(parents=True)
        scan_path.write_bytes(scan)
        self.patches = [
            patch("src.public_2023_history_audit.EXPECTED_2023_PLUS_ROWS", 30),
            patch("src.public_2023_history_audit.EXPECTED_FROZEN_SCAN_SHA256", hashlib.sha256(scan).hexdigest()),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self._write_sources()

    def tearDown(self):
        self.temp.cleanup()

    def _csv_bytes(self, timeframe: str, *, rows: tuple[str, ...] = TIMES, spread: str = "0") -> bytes:
        del timeframe  # The auditor must preserve open timestamps without assuming a fixed session grid.
        lines = [",".join(REQUIRED_COLUMNS)]
        for time in rows:
            lines.append(f"{time},1.1000,1.1010,1.0990,1.1005,10,{spread},0")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _write_sources(self, *, expected_crlf_hash: bool = False) -> dict[str, bytes]:
        items = []
        originals = {}
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                relative = f"data/market/{symbol}/{timeframe}.csv"
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                raw = self._csv_bytes(timeframe)
                path.write_bytes(raw)
                originals[relative] = raw
                declared = raw.replace(b"\n", b"\r\n") if expected_crlf_hash else raw
                items.append({
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "rows_after_2023plus": 2,
                    "first_2023plus_utc": TIMES[0],
                    "last_closed_utc": TIMES[1],
                    "effective_cutoff_utc": TIMES[1],
                    "sha256": hashlib.sha256(declared).hexdigest(),
                    "unconfirmed_intervals": 1,
                })
        report = {
            "status": "COMPLETE_VS_MT5_2023_PLUS",
            "target_start_utc": "2023-01-01T00:00:00+00:00",
            "as_of_utc": AS_OF,
            "items": items,
            "aggregate": {"final_2023plus_rows": 30},
        }
        report_path = self.root / "reports/phase2_2_2g/validation_summary.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return originals

    def _audit(self):
        return audit_public_2023_history(self.root, base_sha=BASE_SHA)

    def test_exact_inventory_raw_and_crlf_hash_lineage_and_read_only(self):
        originals = self._write_sources(expected_crlf_hash=True)
        before = {path: (self.root / path).read_bytes() for path in originals}
        report = self._audit()
        self.assertEqual(report["status"], "SOURCE_INTEGRITY_PASS")
        self.assertEqual(report["inventory_count_actual"], 15)
        self.assertEqual(report["aggregate_rows_actual"], 30)
        self.assertEqual(report["frozen_scan"]["status"], "PASS")
        first = report["series"][0]
        self.assertEqual(first["phase_g_hash_comparison"], "CRLF_EOL_EQUIVALENT")
        self.assertEqual(first["spread_quality"]["row_classification_counts"]["OBSERVED_ZERO_UNVERIFIED"], 2)
        self.assertEqual(first["spread_quality"]["execution_cost_claim"], "UNVERIFIED_BROKER_COSTS_NOT_PRESENT_IN_THIS_FILE")
        self.assertEqual(before, {path: (self.root / path).read_bytes() for path in originals})
        self.assertEqual(report["calendar_status"], "UNKNOWN_BROKER_CALENDAR_NOT_VERIFIED")
        self.assertIsNone(first["cadence"]["inferred_missing_bar_count"])

    def test_changed_ohlc_timestamp_spread_or_row_is_blocked(self):
        for change, expected_code in (
            (b"1.1005", b"1.1006"),
            (TIMES[0].encode(), b"2023-01-02T00:05:00+00:00"),
            (b",0,0\n", b",-1,0\n"),
            (b"2023-01-03T00:00:00+00:00", b"2023-01-03T00:00:00+00:00"),
        ):
            with self.subTest(change=change):
                self._write_sources()
                path = self.root / "data/market/EURUSD/M5.csv"
                raw = path.read_bytes()
                if change == b"2023-01-03T00:00:00+00:00":
                    raw = raw.rsplit(b"\n", 2)[0] + b"\n"
                else:
                    raw = raw.replace(change, expected_code, 1)
                path.write_bytes(raw)
                report = self._audit()
                self.assertEqual(report["status"], "DATA_INTEGRITY_BLOCKED")
                self.assertTrue(any(error.endswith(":EURUSD:M5") for error in report["errors"]))

    def test_nonfinite_ohlc_and_negative_spread_are_rejected(self):
        self._write_sources()
        path = self.root / "data/market/EURUSD/M5.csv"
        path.write_bytes(path.read_bytes().replace(b",1.1000,", b",NaN,"))
        report = self._audit()
        self.assertEqual(report["status"], "DATA_INTEGRITY_BLOCKED")
        self.assertEqual(report["series"][0]["counts"]["invalid_ohlc_rows"], 2)
        self._write_sources()
        path.write_bytes(path.read_bytes().replace(b",0,0\n", b",-2,0\n"))
        report = self._audit()
        self.assertEqual(report["series"][0]["counts"]["invalid_spread_rows"], 2)

    def test_unauthorized_time_period_is_blocked(self):
        self._write_sources()
        path = self.root / "data/market/EURUSD/M5.csv"
        path.write_bytes(path.read_bytes().replace(TIMES[0].encode(), b"2022-12-31T23:55:00+00:00"))
        report = self._audit()
        self.assertEqual(report["status"], "DATA_INTEGRITY_BLOCKED")
        self.assertGreater(report["series"][0]["counts"]["pre_2023_rows"], 0)

    def test_duplicate_timestamp_is_blocked_without_selecting_a_duplicate_row(self):
        self._write_sources()
        path = self.root / "data/market/EURUSD/M5.csv"
        path.write_bytes(path.read_bytes().replace(TIMES[1].encode(), TIMES[0].encode()))
        report = self._audit()
        self.assertEqual(report["status"], "DATA_INTEGRITY_BLOCKED")
        self.assertEqual(report["series"][0]["counts"]["duplicate_timestamps"], 1)

    def test_manifest_binds_current_bytes_and_rejects_post_manifest_change(self):
        audit = self._audit()
        manifest = build_public_2023_source_manifest(audit)
        passed = validate_public_2023_source_manifest(self.root, manifest, audit)
        self.assertEqual(passed["status"], "PASS")
        path = self.root / "data/market/EURUSD/M5.csv"
        path.write_bytes(path.read_bytes().replace(b"1.1005", b"1.1006"))
        with self.assertRaisesRegex(ValueError, "FRESH_AUDIT_MISMATCH|SOURCE_HASH_CHANGED"):
            validate_public_2023_source_manifest(self.root, manifest, audit)

    def test_manifest_is_portable_across_crlf_only_rewrite(self):
        self._write_sources(expected_crlf_hash=True)
        audit_lf = self._audit()
        manifest = build_public_2023_source_manifest(audit_lf)
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                path = self.root / "data/market" / symbol / f"{timeframe}.csv"
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        result = validate_public_2023_source_manifest(self.root, manifest, audit_lf)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["actual_files_rehashed"], 15)

    def test_manifest_rejects_forged_audit_summary(self):
        audit = self._audit()
        manifest = build_public_2023_source_manifest(audit)
        forged_audit = json.loads(json.dumps(audit))
        forged_audit["series"][0]["row_count"] += 1
        with self.assertRaisesRegex(ValueError, "FRESH_AUDIT_MISMATCH"):
            validate_public_2023_source_manifest(self.root, manifest, forged_audit)

    def test_manifest_rejects_path_escape_and_fake_synthetic_provenance(self):
        audit = self._audit()
        manifest = build_public_2023_source_manifest(audit)
        unsafe = json.loads(json.dumps(manifest))
        unsafe["source_manifest"]["sources"][0]["path"] = "../outside.csv"
        with self.assertRaises(ValueError):
            validate_public_2023_source_manifest(self.root, unsafe, audit)
        fake = json.loads(json.dumps(manifest))
        fake["dataset_kind"] = "SYNTHETIC_TEST_ONLY"
        with self.assertRaisesRegex(ValueError, "DATA_CLASS_INVALID"):
            validate_public_2023_source_manifest(self.root, fake, audit)

    def test_rehashed_manifest_cannot_forge_the_original_raw_sha(self):
        audit = self._audit()
        manifest = build_public_2023_source_manifest(audit)
        manifest["source_manifest"]["sources"][0]["raw_sha256"] = "f" * 64
        sources = manifest["source_manifest"]["sources"]
        manifest["source_manifest"]["source_manifest_sha256"] = hashlib.sha256(
            json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest["source_manifest_sha256"] = manifest["source_manifest"]["source_manifest_sha256"]
        unhashed = dict(manifest)
        unhashed.pop("manifest_sha256")
        manifest["manifest_sha256"] = hashlib.sha256(
            json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_CHANGED"):
            validate_public_2023_source_manifest(self.root, manifest, audit)

    def test_missing_series_and_symlink_are_blocked(self):
        path = self.root / "data/market/EURUSD/M5.csv"
        path.unlink()
        self.assertEqual(self._audit()["status"], "DATA_INTEGRITY_BLOCKED")
        self._write_sources()
        outside = self.root / "outside.csv"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        report = self._audit()
        self.assertEqual(report["status"], "DATA_INTEGRITY_BLOCKED")
        self.assertTrue(any("PUBLIC_2023_SOURCE_PATH_UNSAFE" in error for error in report["errors"]))

    def test_public_real_data_loader_stays_closed_without_official_calendar(self):
        audit = self._audit()
        manifest = build_public_2023_source_manifest(audit)
        with self.assertRaisesRegex(ValueError, "BROKER_CALENDAR_UNKNOWN"):
            load_public_2023_verified_dataset(
                self.root, manifest, audit, None,
                warmup_start_utc="2023-01-01T00:00:00Z",
                evaluation_start_utc="2023-01-02T00:00:00Z",
                evaluation_end_utc="2023-01-03T00:00:00Z",
                observation_end_utc="2023-01-04T00:00:00Z",
                as_of_utc=AS_OF,
            )

    def test_synthetic_five_timeframe_parity_with_warmup_dst_gap_and_partial_bar(self):
        decision = pd.Timestamp("2026-03-09T08:00:00Z").to_pydatetime()
        with patch("tests.test_verified_dataset_backtests.DECISION", decision):
            frames = synthetic_frames()
        m5 = frames["EURUSD"]["M5"]
        dst_gap_open = pd.Timestamp("2026-03-08T07:00:00Z")
        self.assertTrue((m5["time"] == dst_gap_open).any())
        # A missing quote around the DST transition is preserved as a data gap;
        # no weekend or broker-calendar closure is inferred by the dataset.
        frames["EURUSD"]["M5"] = m5.loc[m5["time"] != dst_gap_open].reset_index(drop=True)
        with patch(
            "src.phase2_2_2d_sessions.TRUSTED_BROKER_SOURCE_HOSTS",
            frozenset({"broker.example.test"}),
        ):
            dataset = VerifiedResearchDataset.synthetic_test_dataset(
                frames,
                synthetic_session_manifest(),
                warmup_start_utc=decision - timedelta(days=365),
                evaluation_start_utc=decision,
                evaluation_end_utc=decision + timedelta(seconds=1),
                observation_end_utc=decision + timedelta(minutes=15),
                as_of_utc=decision + timedelta(minutes=15),
            )
            self.assertGreater(
                dataset.public_manifest["period_bar_count_by_symbol_timeframe"]["EURUSD"]["D1"]["warmup"],
                200,
            )
            self.assertEqual(
                dataset.public_manifest["partial_bars_excluded_by_symbol_timeframe"]["EURUSD"]["M5"],
                1,
            )
            scan = dataset.scan_symbol("EURUSD")
            dataset.assert_scan_parity(scan, symbol="EURUSD")
            self.assertEqual(scan["dataset_fingerprint"], dataset.public_manifest["dataset_fingerprint"])
            history = dataset.prepare_backtest_history("EURUSD")
            state = build_historical_market_state("EURUSD", decision, history)
            plan = build_historical_trade_plan("EURUSD", "BUY", "STOP", state)
            self.assertEqual(plan["status"], "VALID")
            execution = simulate_trade(
                plan, decision, history["M5"], cost_scenario="BASE",
                risk_amount_eur=100.0, spread_floor_points=0.0,
            )
            self.assertIn(execution["result"], {"AMBIGUOUS_ENTRY_BAR", "AMBIGUOUS_BOTH", "WIN", "LOSS"})
            self.assertEqual(dataset.public_manifest["economics_eligible"], False)


if __name__ == "__main__":
    unittest.main()
