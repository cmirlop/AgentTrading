from __future__ import annotations

import ast
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import backfill_market_history
from src.market_history_audit import (
    SYMBOLS,
    TIMEFRAMES_SECONDS,
    _compare_overlap,
    _gap_record,
    _hash_integrity,
    _load_csv,
    _merge_record_variants,
    audit_market_history,
)
from src.market_history_backfill import (
    _source_hashes,
    execute_backfill_plan,
    plan_chunks,
    safe_copy_rates_range,
    validate_mt5_rates,
    validate_backfill_run,
    verify_real_readonly_account,
)
from src.history_data_integrity import (
    CANONICAL_HASH_POLICY,
    backfill_run_paths,
    canonicalize_csv_line_endings,
    content_hashes,
    validate_run_id,
)
from src.spread_quality import audit_spread_timeline, classify_spread


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def _rate(time_value: datetime | int, *, close: float = 1.101, spread: int = 3) -> dict:
    epoch = int(time_value.timestamp()) if isinstance(time_value, datetime) else time_value
    return {"time": epoch, "open": 1.100, "high": max(1.102, close), "low": 1.099, "close": close, "tick_volume": 100, "spread": spread, "real_volume": 0}


class HistoryGapAuditTests(unittest.TestCase):
    def test_exact_missing_slots_and_weekend_classification(self) -> None:
        left_time = datetime(2026, 10, 16, 23, 55, tzinfo=UTC)
        right_time = datetime(2026, 10, 18, 0, 10, tzinfo=UTC)
        gap = _gap_record({"time": left_time}, {"time": right_time}, 300)
        self.assertIsNotNone(gap)
        self.assertEqual(gap["missing_start_utc"], "2026-10-17T00:00:00+00:00")
        self.assertEqual(gap["missing_end_utc"], "2026-10-18T00:05:00+00:00")
        self.assertEqual(gap["missing_bar_count"], 290)
        self.assertEqual(gap["classification_ranges"][0]["classification"], "EXPECTED_WEEKEND_CLOSED_SATURDAY")
        self.assertEqual(gap["classification_ranges"][1]["classification"], "PENDING_WEEKLY_SESSION_SUNDAY")
        self.assertFalse(gap["recovery_candidate"])

    def test_weekday_hole_is_pending_not_assumed_to_be_an_error(self) -> None:
        left = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
        right = left + timedelta(minutes=20)
        gap = _gap_record({"time": left}, {"time": right}, 300)
        self.assertEqual(gap["classification"], "PENDING_MARKET_CALENDAR")
        self.assertEqual(gap["classification_ranges"][0]["classification"], "PENDING_TRADING_CALENDAR_OR_DATA_GAP")
        self.assertTrue(gap["recovery_candidate"])

    def test_irregular_timestamp_spacing_has_no_invented_missing_slots(self) -> None:
        left = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
        right = left + timedelta(minutes=6)
        gap = _gap_record({"time": left}, {"time": right}, 300)
        self.assertEqual(gap["classification"], "PENDING_IRREGULAR_TIMEFRAME_CADENCE")
        self.assertIsNone(gap["missing_start_utc"])
        self.assertIsNone(gap["missing_bar_count"])
        self.assertFalse(gap["recovery_candidate"])

    def test_overlap_union_deduplicates_equal_bars_and_keeps_conflicts(self) -> None:
        t = datetime(2026, 1, 1, tzinfo=UTC)
        base = {"time": t, "signature": ("1.0", "1.1", "0.9", "1.05", "10", "2", "0"), "row": {"open": "1.0", "high": "1.1", "low": "0.9", "close": "1.05"}}
        same = {**base, "row": dict(base["row"])}
        different = {"time": t, "signature": ("1.0", "1.1", "0.9", "1.06", "10", "2", "0"), "row": {"open": "1.0", "high": "1.1", "low": "0.9", "close": "1.06"}}
        merged = _merge_record_variants([base], [same, different])
        self.assertEqual(len(merged), 2)
        comparison = _compare_overlap([base], [different])
        self.assertEqual(comparison["shared_timestamp_count"], 1)
        self.assertEqual(comparison["price_conflict_count"], 1)

    def test_manifest_hash_accounts_for_crlf_checkout_normalization(self) -> None:
        content = b"time,open\r\n2026-01-01,1\r\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.csv"
            path.write_bytes(content.replace(b"\r\n", b"\n"))
            result = _hash_integrity(path, hashlib.sha256(content).hexdigest())
        self.assertEqual(result["status"], "PASS_LEGACY_CRLF_LINE_ENDING_MATCH")
        self.assertFalse(result["raw_hash_matches_manifest"])
        self.assertTrue(result["legacy_crlf_variant_matches_manifest"])
        self.assertFalse(result["canonical_hash_matches_manifest"])

    def test_ohlc_integrity_is_independent_from_spread_quality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            path.write_text(
                "time,open,high,low,close,tick_volume,spread,real_volume\n"
                "2026-01-01T00:00:00+00:00,1.1,1.2,1.0,1.15,3,,0\n",
                encoding="utf-8",
            )
            result = _load_csv(path, "M5", datetime(2026, 1, 2, tzinfo=UTC))
        self.assertEqual(result["ohlc_integrity_status"], "PASS")
        self.assertEqual(result["spread_audit"]["quality_counts"]["MISSING"], 1)

    def test_actual_frozen_sources_have_exact_known_bridges_and_remain_unchanged(self) -> None:
        report, spread, plan = audit_market_history(ROOT, as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
        self.assertEqual(report["snapshot_file_inventory"]["actual_csv_count"], 15)
        self.assertTrue(report["source_files_unchanged"])
        self.assertEqual(report["critical_integrity_errors"], [])
        self.assertEqual(report["counts"]["price_overlap_conflicts"], 0)
        self.assertEqual(plan["status"], "HISTORY_BACKFILL_PREPARED")
        self.assertFalse(plan["download_command_executed_in_this_phase"])
        for symbol in ("EURUSD", "USDCAD", "AUDUSD"):
            m5 = report["symbols"][symbol]["M5"]["transition"]["bridge"]
            m15 = report["symbols"][symbol]["M15"]["transition"]["bridge"]
            self.assertEqual(m5["missing_start_utc"], "2026-10-12T06:40:00+00:00")
            self.assertEqual(m5["missing_end_utc"], "2026-11-04T11:00:00+00:00")
            self.assertEqual(m5["missing_bar_count"], 6677)
            self.assertEqual(m15["missing_start_utc"], "2026-10-12T06:30:00+00:00")
            self.assertEqual(m15["missing_end_utc"], "2026-10-26T12:15:00+00:00")
            self.assertEqual(m15["missing_bar_count"], 1368)
            for timeframe in ("M5", "M15", "H1", "H4", "D1"):
                source = report["symbols"][symbol][timeframe]
                self.assertEqual(source["previous_source"]["ohlc_integrity_status"], "PASS")
                self.assertEqual(source["recent_source"]["ohlc_integrity_status"], "PASS")
                self.assertFalse(spread["by_symbol_timeframe"][symbol][timeframe]["execution_inference_eligible"])


class SpreadAuditTests(unittest.TestCase):
    def test_spread_states_and_session_distribution(self) -> None:
        self.assertEqual(classify_spread(0), "OBSERVED_ZERO_UNVERIFIED")
        self.assertEqual(classify_spread(2), "OBSERVED_VALID")
        self.assertEqual(classify_spread(None), "MISSING")
        self.assertEqual(classify_spread(-1), "INVALID")
        t = datetime(2026, 1, 5, 8, tzinfo=UTC)
        result = audit_spread_timeline([(t, 0), (t + timedelta(hours=6), 4), (t + timedelta(hours=12), None), (t + timedelta(hours=16), -2)])
        self.assertEqual(result["quality_counts"]["OBSERVED_ZERO_UNVERIFIED"], 1)
        self.assertEqual(result["quality_counts"]["OBSERVED_VALID"], 1)
        self.assertEqual(result["quality_counts"]["MISSING"], 1)
        self.assertEqual(result["quality_counts"]["INVALID"], 1)
        self.assertEqual(result["by_utc_session"]["LONDON"]["records"], 1)
        self.assertFalse(result["imputation_applied"])

    def test_frozen_sources_have_separate_old_new_and_union_statistics(self) -> None:
        _, spread, _ = audit_market_history(ROOT, as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
        expected = {"EURUSD": 94.9744, "AUDUSD": 94.9744, "USDCAD": 32.9231}
        for symbol, zero_percent in expected.items():
            actual = spread["by_symbol_timeframe"][symbol]["M5"]["recent_source"]["zero_percent"]
            self.assertAlmostEqual(actual, zero_percent, places=4)
        self.assertTrue(spread["current_snapshot_not_extrapolated_to_history"])
        self.assertEqual(spread["current_mt5_readonly_snapshot"]["values_by_symbol"]["EURUSD"]["spread_points"], 8.0)


class BackfillPlanningTests(unittest.TestCase):
    def test_large_ranges_split_under_bar_cap_with_overlaps(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        end = start + timedelta(seconds=2996 * 300)
        chunks = plan_chunks([{
            "symbol": "EURUSD", "timeframe": "M5",
            "missing_start_utc": start.isoformat(), "missing_end_utc": end.isoformat(),
        }])
        self.assertEqual(len(chunks), 4)
        for chunk in chunks:
            requested = int((datetime.fromisoformat(chunk["request_end_utc"]) - datetime.fromisoformat(chunk["request_start_utc"])).total_seconds() / 300) + 1
            self.assertLessEqual(requested, 1000)
            self.assertEqual(chunk["overlap_bars_each_side"], 2)

    def test_invalid_symbol_or_unaligned_interval_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plan_chunks([{"symbol": "GBPUSD", "timeframe": "M5", "missing_start_utc": "2025-01-01T00:00:00+00:00", "missing_end_utc": "2025-01-01T00:00:00+00:00"}])
        with self.assertRaises(ValueError):
            plan_chunks([{"symbol": "EURUSD", "timeframe": "M5", "missing_start_utc": "2025-01-01T00:00:00+00:00", "missing_end_utc": "2025-01-01T00:01:00+00:00"}])

    def test_open_bars_are_excluded_and_duplicates_or_bad_ohlc_are_classified(self) -> None:
        as_of = datetime(2026, 11, 11, 0, 2, tzinfo=UTC)
        closed = _rate(datetime(2026, 1, 1, tzinfo=UTC))
        open_bar = _rate(datetime(2026, 11, 11, 0, 0, tzinfo=UTC))
        invalid = _rate(datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
        invalid["high"] = 1.0
        conflicting = _rate(datetime(2026, 1, 1, tzinfo=UTC), close=1.1015)
        result = validate_mt5_rates([closed, dict(closed), conflicting, open_bar, invalid], "M5", as_of_utc=as_of)
        self.assertEqual(result["open_or_future_bar_count"], 1)
        self.assertEqual(result["identical_duplicate_count"], 1)
        self.assertEqual(result["conflicting_duplicate_timestamp_count"], 1)
        self.assertEqual(result["invalid_bar_count"], 1)
        self.assertEqual(len(result["bars"]), 2)

    def test_mt5_read_is_guarded_before_and_after_call(self) -> None:
        api = _FakeApi()
        chunk = {
            "symbol": "EURUSD", "timeframe": "M5",
            "request_start_utc": "2025-01-01T00:00:00+00:00",
            "request_end_utc": "2025-01-01T00:05:00+00:00",
        }
        safe_copy_rates_range(api, chunk, api.TIMEFRAME_M5)
        self.assertEqual(api.calls, ["terminal_info", "copy_rates_range", "terminal_info"])

    def test_gate_is_rechecked_after_a_failed_mt5_read(self) -> None:
        api = _FakeApi(fail_on_call=1, disable_gate_on_failure=True)
        chunk = {
            "symbol": "EURUSD", "timeframe": "M5",
            "request_start_utc": "2025-01-01T00:00:00+00:00",
            "request_end_utc": "2025-01-01T00:05:00+00:00",
        }
        with self.assertRaisesRegex(Exception, "TRADEAPI_DISABLED_NOT_VERIFIED_TRUE"):
            safe_copy_rates_range(api, chunk, api.TIMEFRAME_M5)
        self.assertEqual(api.calls, ["terminal_info", "copy_rates_range", "terminal_info"])

    def test_demo_account_is_rejected_before_any_rates_read(self) -> None:
        api = _FakeApi(trade_mode=0)
        with self.assertRaisesRegex(Exception, "ACCOUNT_NOT_VERIFIED_REAL"):
            verify_real_readonly_account(api)
        self.assertEqual(api.copy_count, 0)

    def test_dry_run_needs_no_mt5_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps({
                "schema_version": 2,
                "canonicalization_policy": CANONICAL_HASH_POLICY,
                "status": "HISTORY_BACKFILL_PREPARED",
                "symbols": ["EURUSD"],
                "timeframes_seconds": {"M5": 300},
                "chunk_policy": {"max_bars_per_request": 1000, "overlap_bars_each_side": 2},
                "source_file_hashes": {},
                "backfill_intervals": [],
            }), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(sys.modules, {"MetaTrader5": None}), redirect_stdout(output):
                code = backfill_market_history.main(["--dry-run", "--audit-plan", str(plan_path)])
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertFalse(report["mt5_imported"])
        self.assertEqual(report["mt5_connections"], 0)

    def test_real_downloader_requires_explicit_confirmation_and_has_no_execution_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan_path = Path(directory) / "plan.json"
            plan_path.write_text(json.dumps({
                "schema_version": 2,
                "canonicalization_policy": CANONICAL_HASH_POLICY,
                "status": "HISTORY_BACKFILL_PREPARED",
                "symbols": ["EURUSD"],
                "timeframes_seconds": {"M5": 300},
                "chunk_policy": {"max_bars_per_request": 1000, "overlap_bars_each_side": 2},
                "source_file_hashes": {},
                "backfill_intervals": [],
                "download_command_executed_in_this_phase": False,
            }), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = backfill_market_history.main(["--download", "--audit-plan", str(plan_path)])
        self.assertEqual(code, 2)
        self.assertIn("EXPLICIT_REAL_READ_ONLY_CONFIRMATION_REQUIRED", output.getvalue())
        paths = [ROOT / "scripts/backfill_market_history.py", ROOT / "src/market_history_backfill.py"]
        forbidden = {"order_send", "order_check", "symbol_select", "login"}
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
            self.assertFalse(calls & forbidden, f"forbidden MT5 methods in {path}: {calls & forbidden}")

    def test_resume_after_interruption_does_not_refetch_completed_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_source_tree(root)
            plan = _test_plan(root, bars=1000)
            api = _FakeApi(fail_on_call=2)
            with self.assertRaisesRegex(RuntimeError, "SIMULATED_INTERRUPTION"):
                execute_backfill_plan(api, plan, root=root, run_id="resume-test", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
            self.assertEqual(api.copy_count, 2)
            retry = _FakeApi()
            result = execute_backfill_plan(retry, plan, root=root, run_id="resume-test", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
            self.assertEqual(retry.copy_count, 1)
            self.assertEqual(result["provider_calls"], 3)
            self.assertEqual(result["status"], "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT")
            self.assertTrue(result["source_files_unchanged"])

    def test_source_conflict_is_preserved_and_blocks_continuity_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            collision_time = datetime(2025, 1, 1, 0, 15, tzinfo=UTC)
            _write_source_tree(root, original_collision=collision_time)
            plan = _test_plan(root, start=collision_time, bars=1)
            api = _FakeApi(response_time=collision_time, response_close=1.109)
            result = execute_backfill_plan(api, plan, root=root, run_id="conflict-test", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
            self.assertEqual(result["status"], "HISTORY_BACKFILL_PREPARED_WITH_REVIEW")
            self.assertEqual(len(result["conflicts"]), 1)
            conflict = result["conflicts"][0]
            self.assertEqual(conflict["action"], "PRESERVED_BOTH_REQUIRES_REVIEW")
            self.assertNotEqual(conflict["downloaded_variant"]["close"], conflict["existing_variants"][0]["close"])
            self.assertTrue(result["source_files_unchanged"])

    def test_stale_source_plan_is_rejected_before_mt5_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_source_tree(root)
            plan = _test_plan(root, bars=1)
            source = root / "data/market/EURUSD/M5.csv"
            source.write_text(source.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            api = _FakeApi()
            with self.assertRaisesRegex(RuntimeError, "BACKFILL_PLAN_SOURCE_CHANGED"):
                execute_backfill_plan(api, plan, root=root, run_id="stale-test", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
            self.assertEqual(api.copy_count, 0)
            self.assertEqual(api.calls, [])


class PortabilityAndPathSafetyTests(unittest.TestCase):
    def test_crlf_and_lf_share_canonical_hash_but_not_raw_hash(self) -> None:
        lf = b"time,open,high,low,close,tick_volume,spread,real_volume\n2025-01-01T00:00:00+00:00,1.1,1.2,1.0,1.15,10,0,0\n"
        crlf = lf.replace(b"\n", b"\r\n")
        lf_hash = content_hashes(lf)
        crlf_hash = content_hashes(crlf)
        self.assertNotEqual(lf_hash["raw_sha256"], crlf_hash["raw_sha256"])
        self.assertEqual(lf_hash["canonical_sha256"], crlf_hash["canonical_sha256"])
        self.assertEqual(canonicalize_csv_line_endings(crlf), lf)

    def test_material_csv_changes_do_not_normalize_away(self) -> None:
        baseline = b"time,open,high,low,close,tick_volume,spread,real_volume\n2025-01-01T00:00:00+00:00,1.1,1.2,1.0,1.15,10,2,0\n2025-01-01T00:05:00+00:00,1.15,1.25,1.05,1.2,11,3,0\n"
        baseline_hash = content_hashes(baseline)["canonical_sha256"]
        changed = {
            "price": baseline.replace(b",1.1,1.2,1.0,1.15,", b",1.1,1.21,1.0,1.15,"),
            "timestamp": baseline.replace(b"2025-01-01T00:00:00+00:00", b"2025-01-01T00:01:00+00:00"),
            "spread": baseline.replace(b",10,2,0", b",10,9,0"),
            "deleted_row": baseline.replace(b"2025-01-01T00:05:00+00:00,1.15,1.25,1.05,1.2,11,3,0\n", b""),
            "truncated": baseline[:-9],
        }
        for label, variant in changed.items():
            with self.subTest(label=label):
                self.assertNotEqual(content_hashes(variant)["canonical_sha256"], baseline_hash)

    def test_plan_generated_on_lf_resumes_with_crlf_sources_and_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_source_tree(root)
            plan = _test_plan(root, bars=1)
            for path in list((root / "data/market").rglob("*.csv")) + list((root / "data/market_history").rglob("*.csv")):
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
            result = execute_backfill_plan(_FakeApi(), plan, root=root, run_id="windows-portable", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
            chunk = next((root / "data/market_history/backfill_runs/windows-portable/chunks").glob("*.csv"))
            chunk.write_bytes(chunk.read_bytes().replace(b"\n", b"\r\n"))
            audit = validate_backfill_run(root, "windows-portable", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
        self.assertTrue(result["source_files_unchanged"])
        self.assertEqual(audit["integrity_status"], "PASS")
        self.assertEqual(audit["chunks"][0]["hash_status"], "CANONICAL_MATCH_RAW_BYTES_DIFFER")

    def test_run_id_validation_rejects_paths_dot_segments_and_reserved_windows_names(self) -> None:
        self.assertEqual(validate_run_id("backfill-20261109"), "backfill-20261109")
        for invalid in (".", "..", "../otra_carpeta", "..\\otra_carpeta", "C:\\temp", "CON", "NUL", "COM1", "LPT9", "a/b", "a\\b"):
            with self.subTest(run_id=invalid), self.assertRaises(ValueError):
                validate_run_id(invalid)

    def test_symlink_run_directory_is_rejected_before_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            _write_source_tree(root)
            plan = _test_plan(root, bars=1)
            runs = root / "data/market_history/backfill_runs"
            runs.mkdir(parents=True)
            try:
                (runs / "evil-run").symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink unavailable on this platform: {type(exc).__name__}")
            api = _FakeApi()
            with self.assertRaisesRegex(ValueError, "BACKFILL_SYMLINK_PATH_REJECTED"):
                execute_backfill_plan(api, plan, root=root, run_id="evil-run", as_of_utc=datetime(2026, 11, 11, tzinfo=UTC))
            self.assertEqual(api.calls, [])
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_extra_or_missing_source_file_rejects_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_source_tree(root)
            extra = root / "data/market/EURUSD/EXTRA.csv"
            extra.write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "BACKFILL_SOURCE_INVENTORY_MISMATCH"):
                _source_hashes(root)
            extra.unlink()
            missing = root / "data/market/EURUSD/M5.csv"
            missing.unlink()
            with self.assertRaisesRegex(RuntimeError, "BACKFILL_SOURCE_MISSING"):
                _source_hashes(root)


class _FakeApi:
    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_M5 = 5

    def __init__(self, *, trade_mode: int = 2, fail_on_call: int | None = None, response_time: datetime | None = None, response_close: float = 1.101, disable_gate_on_failure: bool = False):
        self.trade_mode = trade_mode
        self.fail_on_call = fail_on_call
        self.response_time = response_time
        self.response_close = response_close
        self.disable_gate_on_failure = disable_gate_on_failure
        self.calls: list[str] = []
        self.copy_count = 0
        self.tradeapi_disabled = True

    def terminal_info(self):
        self.calls.append("terminal_info")
        return {"connected": True, "tradeapi_disabled": self.tradeapi_disabled}

    def account_info(self):
        self.calls.append("account_info")
        return {"trade_mode": self.trade_mode, "currency": "EUR"}

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.calls.append("copy_rates_range")
        self.copy_count += 1
        if self.fail_on_call == self.copy_count:
            if self.disable_gate_on_failure:
                self.tradeapi_disabled = False
            raise RuntimeError("SIMULATED_INTERRUPTION")
        timestamp = self.response_time or start
        if not start <= timestamp <= end:
            timestamp = start
        return [_rate(timestamp, close=self.response_close)]


def _write_source_tree(root: Path, *, original_collision: datetime | None = None) -> None:
    for symbol in ("EURUSD", "USDCAD", "AUDUSD"):
        for timeframe in ("M5", "M15", "H1", "H4", "D1"):
            old = root / "data/market" / symbol / f"{timeframe}.csv"
            recent = root / "data/market_history/mt5_real_readonly/20261109T202112.171333Z" / f"{symbol}_{timeframe}.csv"
            old.parent.mkdir(parents=True, exist_ok=True)
            recent.parent.mkdir(parents=True, exist_ok=True)
            header = "time,open,high,low,close,tick_volume,spread,real_volume\n"
            content = header
            if original_collision and symbol == "EURUSD" and timeframe == "M5":
                content += f"{original_collision.isoformat()},1.1,1.102,1.099,1.101,100,2,0\n"
            old.write_text(content, encoding="utf-8")
            recent.write_text(header, encoding="utf-8")


def _test_plan(root: Path, *, start: datetime | None = None, bars: int = 1) -> dict:
    start = start or datetime(2025, 1, 1, 0, 15, tzinfo=UTC)
    end = start + timedelta(minutes=5 * (bars - 1))
    intervals = [{
        "symbol": "EURUSD", "timeframe": "M5", "interval_id": "test-interval",
        "missing_start_utc": start.isoformat(), "missing_end_utc": end.isoformat(),
    }]
    return {
        "schema_version": 2,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "status": "HISTORY_BACKFILL_PREPARED",
        "backfill_intervals": intervals,
        "source_file_hashes": _source_hashes(root),
        "symbols": list(SYMBOLS),
        "timeframes_seconds": dict(TIMEFRAMES_SECONDS),
        "chunk_policy": {"max_bars_per_request": 1000, "overlap_bars_each_side": 2},
        "download_command_executed_in_this_phase": False,
    }


if __name__ == "__main__":
    unittest.main()
