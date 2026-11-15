from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from config.market_config import SYMBOLS, TIMEFRAMES
from src.phase2_2_2d_sessions import TIMEFRAME_SECONDS
from src.history_data_integrity import CANONICAL_HASH_POLICY, file_content_hashes
from src.historical_state import prepare_symbol_history, prepare_symbol_history_from_frames
from src.market_history_audit import REQUIRED_COLUMNS
from src.verified_history_scan import (
    build_history_source_manifest,
    load_verified_history_frames,
    scan_symbol_from_verified_frames,
    validate_history_source_manifest,
    _safe_relative_path,
)


def _source_manifest(paths):
    sources = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            sources.append({
                "path": f"data/market/{symbol}/{timeframe}.csv",
                "symbol": symbol,
                "timeframe": timeframe,
                "source_kind": "SYNTHETIC",
                "raw_sha256": "b" * 64,
                "canonical_sha256": "c" * 64,
            })
    return {
        "schema_version": 1,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "source_integrity_status": "PASS",
        "run_id": "synthetic",
        "source_manifest_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "source_count": len(sources),
        "symbol_timeframe_coverage": {symbol: list(TIMEFRAMES) for symbol in SYMBOLS},
        "sources": sources,
    }


class VerifiedHistoryScanTests(unittest.TestCase):
    @staticmethod
    def _write_closed_history_fixture(root: Path, *, duplicate_spread: bool = False, conflicting_ohlc: bool = False):
        inventory = []
        base_times = {
            "M5": ["2025-01-06T00:00:00Z", "2025-01-06T00:05:00Z", "2025-01-07T00:00:00Z"],
            "M15": ["2025-01-06T00:00:00Z", "2025-01-06T00:15:00Z", "2025-01-07T00:00:00Z"],
            "H1": ["2025-01-06T00:00:00Z", "2025-01-06T01:00:00Z", "2025-01-07T00:00:00Z"],
            "H4": ["2025-01-06T00:00:00Z", "2025-01-06T04:00:00Z", "2025-01-07T00:00:00Z"],
            "D1": ["2025-01-06T00:00:00Z", "2025-01-07T00:00:00Z"],
        }

        def write(path, timeframe, *, changed_close=False, changed_spread=False):
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = [",".join(REQUIRED_COLUMNS)]
            for stamp in base_times[timeframe]:
                close = "1.15" if changed_close and stamp == base_times[timeframe][0] else "1.1"
                spread = "4" if changed_spread else "2"
                lines.append(f"{stamp},1.0,1.2,0.9,{close},10,{spread},0")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            inventory.append({"path": path.relative_to(root).as_posix(), **file_content_hashes(path)})

        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                write(root / "data" / "market" / symbol / f"{timeframe}.csv", timeframe)
        if duplicate_spread or conflicting_ohlc:
            write(
                root / "data" / "market_history" / "mt5_real_readonly" / "synthetic" / "EURUSD_M5.csv",
                "M5",
                changed_close=conflicting_ohlc,
                changed_spread=duplicate_spread,
            )
        source_report = {"status": "PASS", "source_hashes_unchanged": True, "source_inventory": inventory}
        return build_history_source_manifest(root, source_report, {"run_id": "synthetic", "chunks": {}})

    @staticmethod
    def _full_day_manifest():
        weekdays = {str(day): [{"start_local": "00:00", "end_local": "24:00"}] for day in range(7)}
        return {
            "schema_version": 1,
            "evidence": {
                "status": "OFFICIAL_BROKER_DOCUMENTED", "official_source": True,
                "source_url": "https://broker.example.test/sessions.pdf", "sha256": "a" * 64,
                "accessed_utc": "2025-01-01T00:00:00Z",
                "effective_from_utc": "2020-01-01T00:00:00Z", "effective_to_utc": "2030-01-01T00:00:00Z",
            },
            "symbols": list(SYMBOLS), "timeframes": list(TIMEFRAMES),
            "timezone_rule": {"mode": "IANA", "name": "UTC"},
            "sessions_complete": True, "exceptions_complete": True,
            "exception_coverage": {"from_local_date": "2020-01-01", "through_local_date": "2030-01-01"},
            "weekly_sessions": weekdays, "exceptions": {},
            "timeframe_boundary_policy": {timeframe: "FIXED_UTC" for timeframe in TIMEFRAME_SECONDS},
        }

    def test_new_frame_adapter_matches_frozen_route_for_same_local_data(self):
        symbol = "EURUSD"
        old = prepare_symbol_history(symbol)
        frames = {
            timeframe: pd.read_csv(Path("data/market") / symbol / f"{timeframe}.csv")
            for timeframe in TIMEFRAMES
        }
        new = prepare_symbol_history_from_frames(frames)
        for timeframe in TIMEFRAMES:
            pd.testing.assert_frame_equal(old[timeframe], new[timeframe], check_dtype=True)

    def test_adapter_preserves_provider_specific_bar_close(self):
        frames = {}
        for timeframe in TIMEFRAMES:
            frames[timeframe] = pd.DataFrame({
                "time": ["2025-03-09T00:00:00Z", "2025-03-10T00:00:00Z"],
                "open": [1.0, 1.1], "high": [1.2, 1.3], "low": [0.9, 1.0], "close": [1.1, 1.2],
                "tick_volume": [10, 11], "spread": [2, 2], "real_volume": [0, 0],
                "bar_close": ["2025-03-09T23:00:00Z", "2025-03-11T00:00:00Z"],
            })
        prepared = prepare_symbol_history_from_frames(frames)
        self.assertEqual(
            prepared["D1"]["bar_close"].iloc[0],
            pd.Timestamp("2025-03-09T23:00:00Z"),
        )

    def test_source_manifest_is_built_only_from_hash_verified_raw_csvs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory = []
            for symbol in SYMBOLS:
                for timeframe in TIMEFRAMES:
                    path = root / "data" / "market" / symbol / f"{timeframe}.csv"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        ",".join(REQUIRED_COLUMNS) + "\n"
                        "2025-01-06T00:00:00+00:00,1,1,1,1,1,0,0\n",
                        encoding="utf-8",
                    )
                    hashes = file_content_hashes(path)
                    inventory.append({"path": path.relative_to(root).as_posix(), **hashes})
            source_integrity = {"status": "PASS", "source_hashes_unchanged": True, "source_inventory": inventory}
            result = build_history_source_manifest(root, source_integrity, {"run_id": "synthetic", "chunks": {}})
            validate_history_source_manifest(result)
            self.assertEqual(result["source_count"], len(SYMBOLS) * len(TIMEFRAMES))

    def test_foreign_run_id_and_symlink_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = "data/market_history/backfill_runs/foreign-run/chunks/chunk.csv"
            with self.assertRaisesRegex(ValueError, "RUN_ID_MISMATCH"):
                build_history_source_manifest(
                    root,
                    {"status": "PASS", "source_hashes_unchanged": True, "source_inventory": []},
                    {
                        "run_id": "expected-run",
                        "chunks": {
                            "chunk": {
                                "file": foreign,
                                "planned_chunk": {"symbol": "EURUSD", "timeframe": "M5"},
                            }
                        },
                    },
                )
            with patch("pathlib.Path.is_symlink", return_value=True):
                with self.assertRaisesRegex(ValueError, "SYMLINK_REJECTED"):
                    _safe_relative_path(root, "data/market/EURUSD/M5.csv")

    def test_frozen_candidate_scan_is_rejected_as_a_source(self):
        candidate = _source_manifest([])
        candidate["sources"][0]["path"] = "data/backtest/technical_candidate_scan.csv"
        candidate["source_manifest_sha256"] = hashlib.sha256(json.dumps(candidate["sources"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError, "FROZEN_SCAN_SOURCE_FORBIDDEN"):
            validate_history_source_manifest(candidate)

    def test_source_manifest_fingerprint_tampering_is_rejected(self):
        candidate = _source_manifest([])
        candidate["sources"][0]["raw_sha256"] = "d" * 64
        with self.assertRaisesRegex(ValueError, "FINGERPRINT_MISMATCH"):
            validate_history_source_manifest(candidate)

    def test_unknown_calendar_blocks_before_any_source_file_read(self):
        candidate = _source_manifest([])
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "BROKER_CALENDAR_UNVERIFIED"):
                load_verified_history_frames(
                    Path(temporary), candidate, None,
                    evaluation_start_utc="2025-01-01T00:00:00Z",
                    evaluation_end_utc="2025-01-02T00:00:00Z",
                    as_of_utc="2025-01-03T00:00:00Z",
                )

    def test_closed_bars_only_and_spread_conflict_is_not_silently_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._write_closed_history_fixture(root, duplicate_spread=True)
            frames, audit = load_verified_history_frames(
                root, source, self._full_day_manifest(),
                evaluation_start_utc="2025-01-06T00:00:00Z",
                evaluation_end_utc="2025-01-06T00:15:00Z",
                as_of_utc="2025-01-07T00:00:00Z",
            )
            self.assertEqual(len(frames["EURUSD"]["M5"]), 2)
            self.assertEqual(audit["unique_bar_count_by_symbol_timeframe"]["EURUSD|M5"], 2)
            self.assertGreater(audit["spread_variant_timestamp_count"], 0)
            self.assertTrue(pd.isna(frames["EURUSD"]["M5"].iloc[0]["spread"]))

    def test_unresolved_ohlc_duplicate_conflict_blocks_verified_scan_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._write_closed_history_fixture(root, conflicting_ohlc=True)
            with self.assertRaisesRegex(ValueError, "VARIANT_CONFLICT_UNRESOLVED"):
                load_verified_history_frames(
                    root, source, self._full_day_manifest(),
                    evaluation_start_utc="2025-01-06T00:00:00Z",
                    evaluation_end_utc="2025-01-06T00:15:00Z",
                    as_of_utc="2025-01-07T00:00:00Z",
                )

    def test_scan_symbol_refuses_unknown_calendar_before_indicator_evaluation(self):
        with self.assertRaisesRegex(ValueError, "BROKER_CALENDAR_UNVERIFIED"):
            scan_symbol_from_verified_frames(
                "EURUSD", {}, None,
                evaluation_start_utc="2025-01-01T00:00:00Z",
                evaluation_end_utc="2025-01-02T00:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
