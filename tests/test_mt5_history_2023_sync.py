from __future__ import annotations

import ast
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config.market_config import SYMBOLS, TIMEFRAMES
from src import mt5_history_2023_sync as sync


UTC = timezone.utc
TF_SECONDS = sync.TIMEFRAMES_SECONDS
TF_CONSTANTS = {name: index + 1 for index, name in enumerate(TIMEFRAMES)}
AS_OF = datetime(2023, 1, 7, tzinfo=UTC)


def _bar(timestamp: datetime, *, spread: int = 7, close: float = 1.1) -> dict:
    return {
        "time": int(timestamp.timestamp()),
        "open": close,
        "high": close + 0.001,
        "low": close - 0.001,
        "close": close + 0.0001,
        "tick_volume": 12,
        "spread": spread,
        "real_volume": 0,
    }


class FakeMT5:
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, series, *, visible=True, trade_mode=2, disabled=True, flip_gate=False, empty=False):
        self.series = series
        self.visible = visible
        self.trade_mode = trade_mode
        self.tradeapi_disabled = disabled
        self.flip_gate = flip_gate
        self.empty = empty
        self.range_calls = []
        self.position_calls = []
        self.symbol_calls = []
        for name, value in TF_CONSTANTS.items():
            setattr(self, f"TIMEFRAME_{name}", value)

    def terminal_info(self):
        return {"connected": True, "tradeapi_disabled": self.tradeapi_disabled}

    def account_info(self):
        return {"trade_mode": self.trade_mode, "currency": "EUR"}

    def symbol_info(self, symbol):
        self.symbol_calls.append(symbol)
        visible = self.visible(symbol) if callable(self.visible) else self.visible
        return {"visible": visible}

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.range_calls.append((symbol, timeframe, start, end))
        if start.tzinfo is None or end.tzinfo is None:
            raise AssertionError("range datetimes must be timezone-aware")
        if self.flip_gate:
            self.tradeapi_disabled = False
        if self.empty:
            return []
        tf = next(name for name, value in TF_CONSTANTS.items() if value == timeframe)
        start_epoch, end_epoch = int(start.timestamp()), int(end.timestamp())
        return [row for row in self.series[(symbol, tf)] if start_epoch <= int(row["time"]) <= end_epoch]

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.position_calls.append((symbol, timeframe, start_pos, count))
        tf = next(name for name, value in TF_CONSTANTS.items() if value == timeframe)
        rows = self.series[(symbol, tf)]
        return rows[-1:] if rows else []

    def last_error(self):
        return (-4401, "synthetic no data")


class MT5History2023Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        frozen = self.root / sync.FROZEN_DATASET_PATH
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_bytes(b"frozen-test-dataset\n")
        self.hash_patch = patch.object(sync, "FROZEN_DATASET_CANONICAL_SHA256", hashlib.sha256(frozen.read_bytes()).hexdigest())
        self.hash_patch.start()
        self.series = {}
        self.times = [
            datetime(1971, 1, 4, tzinfo=UTC),
            datetime(2023, 1, 1, tzinfo=UTC),
            datetime(2023, 1, 2, tzinfo=UTC),
            datetime(2023, 1, 3, tzinfo=UTC),
            datetime(2023, 1, 4, tzinfo=UTC),
        ]
        self.as_of = AS_OF
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                rows = [_bar(timestamp, close=1.1 + i * 0.01) for i, timestamp in enumerate(self.times)]
                self.series[(symbol, timeframe)] = rows
                path = self.root / "data" / "market" / symbol / f"{timeframe}.csv"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=sync.REQUIRED_COLUMNS, lineterminator="\n")
                    writer.writeheader()
                    for row in rows[:4]:
                        writer.writerow({"time": datetime.fromtimestamp(row["time"], UTC).isoformat(), **{k: row[k] for k in sync.REQUIRED_COLUMNS[1:]}})

    def tearDown(self):
        self.hash_patch.stop()
        self.temp.cleanup()

    def _audit(self, run_id="test_2023_run", *, api=None, progress=None, as_of=None):
        return sync.audit_provider(
            api or FakeMT5(self.series),
            root=self.root,
            run_id=run_id,
            as_of_utc=as_of or self.as_of,
            batch_call_limit=150,
            progress=progress,
        )

    def test_audit_apply_filters_pre2023_and_uses_inclusive_2023_start(self):
        api = FakeMT5(self.series)
        audit = self._audit(api=api)
        self.assertEqual(audit["status"], "COMPLETE_VS_MT5_2023_PLUS")
        self.assertTrue(api.range_calls)
        self.assertTrue(all(start >= sync.TARGET_START for _, _, start, _ in api.range_calls))
        self.assertEqual(api.range_calls[0][2], sync.TARGET_START)
        summary = sync.apply_run(self.root, "test_2023_run")
        self.assertEqual(summary["status"], "COMPLETE_VS_MT5_2023_PLUS")
        self.assertEqual(len(summary["items"]), 15)
        counts = [(item["symbol"], item["timeframe"], item["rows_after_2023plus"], item["provider_coverage"]["rows_closed_observed"], item["last_closed_utc"], item["status"]) for item in summary["items"]]
        self.assertTrue(all(item["rows_after_2023plus"] == 4 for item in summary["items"]), counts)
        self.assertTrue(all(item["pre2023_rows_archived"] == 1 for item in summary["items"]))
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                path = self.root / "data" / "market" / symbol / f"{timeframe}.csv"
                with path.open(newline="", encoding="utf-8") as handle:
                    data = list(csv.DictReader(handle))
                self.assertTrue(all(datetime.fromisoformat(row["time"]) >= sync.TARGET_START for row in data))
                self.assertEqual(len(data), 4)
        backup = self.root / sync.PRIVATE_BASE / "test_2023_run" / "original_csv_backup" / "EURUSD_M5.csv"
        self.assertIn("1971-01-04", backup.read_text(encoding="utf-8"))

    def test_d1_and_h4_dst_open_times_are_not_rejected_by_utc_grid(self):
        d1 = _bar(datetime(2026, 3, 29, 23, tzinfo=UTC))
        h4 = _bar(datetime(2026, 3, 29, 23, tzinfo=UTC))
        self.assertEqual(sync._normalize_rate(d1)["time"], datetime(2026, 3, 29, 23, tzinfo=UTC))
        self.assertEqual(sync._normalize_rate(h4)["time"], datetime(2026, 3, 29, 23, tzinfo=UTC))
        self.assertFalse(sync._closed_at_cutoff(d1_time := datetime(2026, 3, 29, 23, tzinfo=UTC), [d1_time], 0, "D1", d1_time + timedelta(hours=25), None))
        self.assertTrue(sync._closed_at_cutoff(d1_time, [d1_time], 0, "D1", d1_time + timedelta(hours=26), None))

    def test_window_edge_probe_splits_suspected_truncated_range(self):
        window = sync._new_window("EURUSD", "M5", sync.TARGET_START, sync.TARGET_START + timedelta(days=365))
        sparse = [_bar(sync.TARGET_START + timedelta(minutes=5 * i)) for i in range(100)]
        self.assertTrue(sync._looks_truncated(window, sparse))
        children = sync._split_window(window)
        self.assertIsNotNone(children)
        self.assertEqual(children[0]["core_start_utc"], window["core_start_utc"])
        self.assertEqual(children[1]["core_end_exclusive_utc"], window["core_end_exclusive_utc"])

    def test_empty_provider_windows_are_pending_data_not_synthetic_bars(self):
        api = FakeMT5(self.series, empty=True)
        result = self._audit(api=api, run_id="empty_provider")
        self.assertEqual(result["status"], "PARTIAL_PROVIDER_DATA_UNAVAILABLE")
        self.assertEqual(len(api.range_calls), 15)
        summary = sync.apply_run(self.root, "empty_provider")
        self.assertEqual(summary["status"], "PARTIAL_PROVIDER_DATA_UNAVAILABLE")
        self.assertTrue(all(item["provider_coverage"]["empty_windows"] > 0 for item in summary["items"]))

    def test_provider_with_no_2023_data_is_not_reported_complete(self):
        self.as_of = datetime(2025, 1, 7, tzinfo=UTC)
        series = {key: [row for row in rows if int(row["time"]) >= int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())] for key, rows in self.series.items()}
        api = FakeMT5(series)
        result = self._audit(api=api, run_id="provider_starts_late", as_of=self.as_of)
        self.assertNotEqual(result["status"], "COMPLETE_VS_MT5_2023_PLUS")
        summary = sync.apply_run(self.root, "provider_starts_late")
        self.assertEqual(summary["status"], "PARTIAL_PROVIDER_DATA_UNAVAILABLE")

    def test_gate_loss_during_range_read_blocks_without_operational_csv_writes(self):
        api = FakeMT5(self.series, flip_gate=True)
        before = self._csv_hashes()
        result = self._audit(api=api, run_id="gate_loss")
        self.assertEqual(result["status"], "BLOCKED_SAFETY")
        self.assertEqual(self._csv_hashes(), before)
        self.assertEqual(len(api.range_calls), 1)

    def test_demo_account_blocks_before_any_range_read(self):
        api = FakeMT5(self.series, trade_mode=0)
        before = self._csv_hashes()
        result = self._audit(api=api, run_id="demo_block")
        self.assertEqual(result["status"], "BLOCKED_SAFETY")
        self.assertEqual(api.range_calls, [])
        self.assertEqual(self._csv_hashes(), before)

    def test_invisible_symbol_is_never_selected_or_queried(self):
        api = FakeMT5(self.series, visible=lambda symbol: symbol != "EURUSD")
        result = self._audit(api=api, run_id="invisible")
        self.assertEqual(result["status"], "PARTIAL_PROVIDER_DATA_UNAVAILABLE")
        self.assertFalse(any(call[0] == "EURUSD" for call in api.range_calls))
        self.assertTrue(any(state["status"] == "BLOCKED_SYMBOL_NOT_VISIBLE" for state in sync._window_states(self.root / sync.PRIVATE_BASE / "invisible")))

    def test_restart_resumes_pending_windows_without_repeating_completed_page(self):
        api = FakeMT5(self.series)
        def stop_after_first_checkpoint(event):
            if event.get("event") == "WINDOW_CHECKPOINT":
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self._audit(api=api, run_id="resume_same_cut", progress=stop_after_first_checkpoint)
        first_calls = list(api.range_calls)
        self.assertEqual(len(first_calls), 1)
        result = sync.audit_provider(api, root=self.root, run_id="resume_same_cut", resume=True, batch_call_limit=150)
        self.assertNotEqual(result["status"], "PARTIAL_EXECUTION_INTERRUPTED")
        self.assertEqual(api.range_calls[0], first_calls[0])
        self.assertEqual(sum(call == first_calls[0] for call in api.range_calls), 1)

    def test_position0_current_bar_is_excluded_but_closed_weekend_history_is_kept(self):
        api = FakeMT5(self.series)
        audit = self._audit(api=api, run_id="open_bar", as_of=datetime(2023, 1, 3, 0, 1, tzinfo=UTC))
        self.assertEqual(audit["status"], "COMPLETE_VS_MT5_2023_PLUS")
        summary = sync.apply_run(self.root, "open_bar")
        for item in summary["items"]:
            self.assertNotEqual(item["last_closed_utc"], datetime(2023, 1, 3, tzinfo=UTC).isoformat())
            self.assertGreaterEqual(item["rows_after_2023plus"], 1)

    def test_chunk_tamper_blocks_apply_before_csv_write(self):
        api = FakeMT5(self.series)
        self._audit(api=api, run_id="tamper")
        before = self._csv_hashes()
        run_root = self.root / sync.PRIVATE_BASE / "tamper"
        chunk = next((run_root / "provider_chunks").glob("EURUSD_M5_*.json"))
        chunk.write_text(chunk.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(sync.History2023Error, "PROVIDER_CHUNK_HASH_MISMATCH"):
            sync.apply_run(self.root, "tamper")
        self.assertEqual(self._csv_hashes(), before)

    def test_second_cached_verify_preserves_original_added_count_and_reports_zero_new_in_pass(self):
        self._audit()
        first = sync.apply_run(self.root, "test_2023_run")
        self.assertTrue(first["aggregate"]["new_2023plus_rows"] >= 0)
        provider_queries = first["provider_queries"]
        verified = sync.verify_run(self.root, "test_2023_run")
        self.assertTrue(verified["idempotency_second_pass"])
        self.assertEqual(verified["idempotency_second_pass_new_bars"], 0)
        self.assertEqual(verified["aggregate"]["new_2023plus_rows"], first["aggregate"]["new_2023plus_rows"])
        self.assertEqual(verified["provider_queries"], provider_queries)

    def test_provider_overlap_equal_duplicates_collapse_and_spread_conflict_is_visible(self):
        run_root, manifest, _inventory = sync._create_run(self.root, "overlap", self.as_of)
        ts = datetime(2023, 1, 2, tzinfo=UTC)
        pair = {"symbol": "EURUSD", "timeframe": "M5", "visible": True, "status": "AUDIT_WINDOWS_COMPLETE", "position0_open_utc": datetime(2023, 1, 4, tzinfo=UTC).isoformat()}
        for start_day, end_day, bar in ((1, 3, _bar(ts)), (2, 4, _bar(ts))):
            state = sync._new_window("EURUSD", "M5", datetime(2023, 1, start_day, tzinfo=UTC), datetime(2023, 1, end_day, tzinfo=UTC))
            state["status"] = "COMPLETE"
            sync._save_window_result(self.root, run_root, state, [bar])
            sync._write_json(self.root, sync._window_state_path(run_root, state["window_id"]), state)
        bars, result = sync._aggregate_provider(self.root, run_root, pair, self.as_of)
        self.assertEqual(result["identical_overlap_duplicates"], 1)
        self.assertEqual(result["provider_conflicts"], 0)
        conflict_state = sync._new_window("EURUSD", "M5", datetime(2023, 1, 2, tzinfo=UTC), datetime(2023, 1, 5, tzinfo=UTC))
        conflict_state["status"] = "COMPLETE"
        sync._save_window_result(self.root, run_root, conflict_state, [_bar(ts, spread=9)])
        sync._write_json(self.root, sync._window_state_path(run_root, conflict_state["window_id"]), conflict_state)
        _bars, result = sync._aggregate_provider(self.root, run_root, pair, self.as_of)
        self.assertEqual(result["provider_conflicts"], 1)
        private_conflict = run_root / "provider_conflicts_EURUSD_M5.json"
        self.assertTrue(private_conflict.exists())

    def test_frozen_dataset_and_pre2023_backup_are_unchanged_by_audit(self):
        before_frozen = hashlib.sha256((self.root / sync.FROZEN_DATASET_PATH).read_bytes()).hexdigest()
        original_hashes = self._csv_hashes()
        self._audit()
        self.assertEqual(hashlib.sha256((self.root / sync.FROZEN_DATASET_PATH).read_bytes()).hexdigest(), before_frozen)
        self.assertEqual(self._csv_hashes(), original_hashes)
        inventory = json.loads((self.root / sync.PRIVATE_BASE / "test_2023_run" / "pre_sync_inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(len(inventory["files"]), 15)
        self.assertTrue(all(item["pre2023_rows"] == 1 for item in inventory["files"]))

    def test_phase_f_growth_is_reconciled_separately_from_phase_g_additions(self):
        old = self.root / "reports" / "private" / "phase2_2_2f" / "20261115T205056Z"
        old.mkdir(parents=True)
        (old / "pre_sync_inventory.json").write_text(json.dumps({"files": [{"row_count": 506242}]}), encoding="utf-8")
        self._audit()
        run_root, _manifest, inventory = sync._load_run(self.root, "test_2023_run")
        reconciled = sync._phase_f_reconciliation(self.root, inventory)
        self.assertEqual(reconciled["rows_before_phase_2_2_2f"], 506242)
        self.assertEqual(reconciled["net_row_growth_in_phase_2_2_2f"], sum(x["rows_before_all_history"] for x in inventory["files"]) - 506242)
        self.assertIn("not counted as", reconciled["interpretation"])

    def test_source_code_contains_no_trade_or_symbol_mutation_route(self):
        source = Path(sync.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        called_attributes = {
            node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertFalse(called_attributes.intersection(sync.FORBIDDEN_METHODS))
        self.assertTrue(sync.REQUIRED_READS.issubset(sync.READ_ONLY_METHODS))

    def _csv_hashes(self):
        return {
            (symbol, timeframe): hashlib.sha256((self.root / "data" / "market" / symbol / f"{timeframe}.csv").read_bytes()).hexdigest()
            for symbol in SYMBOLS for timeframe in TIMEFRAMES
        }


if __name__ == "__main__":
    unittest.main()

