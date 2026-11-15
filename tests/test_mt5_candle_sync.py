from __future__ import annotations

import ast
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config.market_config import SYMBOLS, TIMEFRAMES
from src import mt5_candle_sync as sync


UTC = timezone.utc
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
TF_CONSTANT = {name: index + 1 for index, name in enumerate(TIMEFRAMES)}


def _bar(timestamp: datetime, close: float = 1.1) -> dict:
    return {
        "time": int(timestamp.timestamp()),
        "open": close,
        "high": close + 0.001,
        "low": close - 0.001,
        "close": close + 0.0001,
        "tick_volume": 12,
        "spread": 7,
        "real_volume": 0,
    }


class FakeMT5:
    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_M5 = TF_CONSTANT["M5"]
    TIMEFRAME_M15 = TF_CONSTANT["M15"]
    TIMEFRAME_H1 = TF_CONSTANT["H1"]
    TIMEFRAME_H4 = TF_CONSTANT["H4"]
    TIMEFRAME_D1 = TF_CONSTANT["D1"]

    def __init__(self, series: dict[tuple[str, str], list[dict]], *, disabled=True, trade_mode=2, visible=True, flip_during_read=False):
        self.series = series
        self.tradeapi_disabled = disabled
        self.trade_mode = trade_mode
        self.visible = visible
        self.flip_during_read = flip_during_read
        self.rate_calls: list[tuple[str, str, int, int]] = []
        self.shutdown_called = False

    def initialize(self):
        return True

    def terminal_info(self):
        return {"connected": True, "tradeapi_disabled": self.tradeapi_disabled}

    def account_info(self):
        return {"trade_mode": self.trade_mode, "currency": "EUR"}

    def symbol_info(self, symbol):
        return {"visible": self.visible}

    def last_error(self):
        return (-4401, "history unavailable")

    def copy_rates_from_pos(self, symbol, timeframe, position, count):
        tf = next(name for name, value in TF_CONSTANT.items() if value == timeframe)
        self.rate_calls.append((symbol, tf, position, count))
        bars = self.series[(symbol, tf)]
        right = max(0, len(bars) - position)
        left = max(0, right - count)
        result = [dict(row) for row in bars[left:right]]
        if self.flip_during_read:
            self.tradeapi_disabled = False
        return result

    def shutdown(self):
        self.shutdown_called = True


class MT5CandleSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.dataset = self.root / sync.FROZEN_DATASET
        self.dataset.parent.mkdir(parents=True, exist_ok=True)
        self.dataset.write_bytes(b"frozen-test-dataset\n")
        digest = hashlib.sha256(self.dataset.read_bytes()).hexdigest()
        self.hash_patch = patch.object(sync, "FROZEN_DATASET_CANONICAL_SHA256", digest)
        self.hash_patch.start()
        self.series: dict[tuple[str, str], list[dict]] = {}
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                if timeframe == "D1":
                    starts = [
                        datetime(2026, 3, 27, 0, tzinfo=UTC),
                        datetime(2026, 3, 28, 0, tzinfo=UTC),
                        datetime(2026, 3, 29, 23, tzinfo=UTC),
                        datetime(2026, 3, 30, 23, tzinfo=UTC),
                        datetime(2026, 3, 31, 23, tzinfo=UTC),
                    ]
                else:
                    start = datetime(2026, 1, 1, tzinfo=UTC)
                    starts = [start + timedelta(seconds=TF_SECONDS[timeframe] * i) for i in range(5)]
                bars = [_bar(ts, 1.1 + index * 0.01) for index, ts in enumerate(starts)]
                self.series[(symbol, timeframe)] = bars
                path = self.root / "data" / "market" / symbol / f"{timeframe}.csv"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=sync.REQUIRED_COLUMNS, lineterminator="\n")
                    writer.writeheader()
                    writer.writerow({
                        "time": datetime.fromtimestamp(bars[0]["time"], UTC).isoformat(),
                        **{key: bars[0][key] for key in sync.REQUIRED_COLUMNS[1:]},
                    })

    def tearDown(self):
        self.hash_patch.stop()
        self.temp.cleanup()

    def test_audit_then_sync_and_second_cached_pass_are_idempotent(self):
        api = FakeMT5(self.series)
        run_id = "sync_test_run"
        as_of = datetime(2026, 4, 5, tzinfo=UTC)
        before = {
            (symbol, timeframe): (self.root / "data" / "market" / symbol / f"{timeframe}.csv").read_bytes()
            for symbol in SYMBOLS for timeframe in TIMEFRAMES
        }
        audited = sync.audit_provider(api, root=self.root, run_id=run_id, as_of_utc=as_of, page_size=3, max_provider_calls=150)
        self.assertEqual(audited["status"], "COMPLETE_VS_MT5_OBSERVED")
        self.assertTrue(api.shutdown_called)
        self.assertEqual(audited["orders_sent"], 0)
        self.assertEqual(audited["trades_executed"], 0)
        for key, raw in before.items():
            path = self.root / "data" / "market" / key[0] / f"{key[1]}.csv"
            self.assertEqual(path.read_bytes(), raw)

        first = sync.sync_from_run(self.root, run_id)
        self.assertEqual(first["status"], "COMPLETE_VS_MT5_OBSERVED")
        self.assertEqual(first["mt5_reads_in_sync_stage"], 0)
        self.assertEqual(first["orders_sent"], 0)
        self.assertEqual(first["trades_executed"], 0)
        self.assertEqual(sum(item["new_rows"] for item in first["items"]), 60)
        second = sync.sync_from_run(self.root, run_id, idempotency_check=True)
        self.assertTrue(second["idempotency_second_pass"])
        self.assertEqual(sum(item["new_rows"] for item in second["items"]), 0)
        self.assertEqual(len(second["items"]), 15)
        summary = sync.public_summary(self.root, run_id, second)
        self.assertEqual(summary["total_bars_added"], 60)
        self.assertEqual(sum(item["new_bars"] for item in summary["items"]), 60)
        self.assertEqual(summary["idempotency_second_pass_new_bars"], 0)

    def test_d1_dst_open_time_is_accepted_without_86400_alignment_assumption(self):
        bar = _bar(datetime(2026, 3, 29, 23, tzinfo=UTC))
        self.assertNotEqual(bar["time"] % 86400, 0)
        normalized = sync._normalize_rate(bar)
        self.assertEqual(normalized["time"], datetime(2026, 3, 29, 23, tzinfo=UTC))

    def test_open_candle_is_excluded_but_closed_weekend_candle_is_kept(self):
        as_of = datetime(2026, 1, 4, 12, tzinfo=UTC)
        closed = _bar(datetime(2026, 1, 2, 21, 55, tzinfo=UTC))
        open_bar = _bar(datetime(2026, 1, 4, 12, 10, tzinfo=UTC))
        bars, invalid, open_count, exact, conflicts = sync._page_bars([closed, open_bar], "M5", as_of)
        self.assertEqual((invalid, open_count, exact, conflicts), (0, 1, 0, []))
        self.assertEqual(len(bars), 2)  # Raw evidence retains it; sync excludes it by as_of.
        self.assertEqual(bars[0]["time"], datetime(2026, 1, 2, 21, 55, tzinfo=UTC))

    def test_tradeapi_disabled_false_blocks_rates_and_csv_writes(self):
        api = FakeMT5(self.series, disabled=False)
        with self.assertRaisesRegex(sync.SafetyGateError, "TRADEAPI_DISABLED_NOT_VERIFIED_TRUE"):
            sync.audit_provider(api, root=self.root, run_id="unsafe_gate", page_size=3)
        self.assertEqual(api.rate_calls, [])
        self.assertTrue(api.shutdown_called)

    def test_real_account_is_required_before_any_rate_read(self):
        api = FakeMT5(self.series, trade_mode=0)
        with self.assertRaisesRegex(sync.SafetyGateError, "ACCOUNT_NOT_VERIFIED_REAL"):
            sync.audit_provider(api, root=self.root, run_id="non_real_gate", page_size=3)
        self.assertEqual(api.rate_calls, [])

    def test_gate_loss_during_read_aborts_before_public_csv_sync(self):
        api = FakeMT5(self.series, flip_during_read=True)
        before = (self.root / "data" / "market" / "EURUSD" / "M5.csv").read_bytes()
        with self.assertRaises(sync.SafetyGateError):
            sync.audit_provider(api, root=self.root, run_id="gate_loss", page_size=3)
        after = (self.root / "data" / "market" / "EURUSD" / "M5.csv").read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(len(api.rate_calls), 1)

    def test_invisible_symbol_is_not_selected_and_is_blocked(self):
        api = FakeMT5(self.series, visible=False)
        result = sync.audit_provider(api, root=self.root, run_id="not_visible", page_size=3)
        self.assertEqual(len(api.rate_calls), 0)
        self.assertTrue(all(result["pairs"][f"AUDUSD|{tf}"]["status"] == "BLOCKED_SYMBOL_NOT_VISIBLE" for tf in TIMEFRAMES))
        source = Path(sync.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertNotIn("symbol_select", calls)

    def test_entrypoint_and_sync_module_have_no_operational_call_or_dynamic_route(self):
        forbidden = {"order_send", "order_check", "login", "symbol_select"}
        for path in (Path(sync.__file__), Path(sync.__file__).parents[1] / "scripts" / "sync_mt5_candles_readonly.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        self.assertNotIn(node.func.attr, forbidden)
                    if isinstance(node.func, ast.Name):
                        self.assertNotIn(node.func.id, forbidden)
                    if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) > 1:
                        if isinstance(node.args[1], ast.Constant):
                            self.assertNotIn(node.args[1].value, forbidden)
                elif isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden)
                elif isinstance(node, ast.Name):
                    self.assertNotIn(node.id, forbidden)

    def test_conflicting_local_provider_bar_is_preserved_and_file_not_overwritten(self):
        symbol, timeframe = SYMBOLS[0], TIMEFRAMES[0]
        path = self.root / "data" / "market" / symbol / f"{timeframe}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=sync.REQUIRED_COLUMNS, lineterminator="\n")
            writer.writeheader()
            conflicting = dict(self.series[(symbol, timeframe)][0])
            conflicting["close"] += 0.0005
            writer.writerow({"time": datetime.fromtimestamp(conflicting["time"], UTC).isoformat(), **{key: conflicting[key] for key in sync.REQUIRED_COLUMNS[1:]}})
        original = path.read_bytes()
        api = FakeMT5(self.series)
        sync.audit_provider(api, root=self.root, run_id="source_conflict", as_of_utc=datetime(2026, 4, 5, tzinfo=UTC), page_size=3)
        result = sync.sync_from_run(self.root, "source_conflict")
        item = next(x for x in result["items"] if x["symbol"] == symbol and x["timeframe"] == timeframe)
        self.assertEqual(item["status"], "PARTIAL_SOURCE_CONFLICT")
        self.assertEqual(path.read_bytes(), original)

    def test_resume_reuses_pinned_cut_and_checkpoints_provider_pages(self):
        api = FakeMT5(self.series)
        run_id = "resume_test"
        first = sync.audit_provider(api, root=self.root, run_id=run_id, as_of_utc=datetime(2026, 4, 5, tzinfo=UTC), page_size=3, max_provider_calls=5)
        self.assertEqual(first["status"], "PARTIAL_PROVIDER_CALL_BUDGET")
        first_manifest = json.loads((self.root / "reports/private/phase2_2_2f" / run_id / "manifest.json").read_text())
        second_api = FakeMT5(self.series)
        resumed = sync.audit_provider(second_api, root=self.root, run_id=run_id, page_size=3, max_provider_calls=5, resume=True)
        self.assertEqual(resumed["as_of_utc"], first_manifest["as_of_utc"])
        self.assertEqual(resumed["provider_calls"], 10)

    def test_private_chunk_hash_tampering_blocks_sync(self):
        api = FakeMT5(self.series)
        sync.audit_provider(api, root=self.root, run_id="tamper_test", as_of_utc=datetime(2026, 4, 5, tzinfo=UTC), page_size=3)
        manifest_path = self.root / "reports/private/phase2_2_2f/tamper_test/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        first_page = manifest["pairs"]["EURUSD|M5"]["pages"][0]
        chunk = self.root / first_page["path"]
        chunk.write_bytes(chunk.read_bytes() + b" ")
        original = (self.root / "data/market/EURUSD/M5.csv").read_bytes()
        result = sync.sync_from_run(self.root, "tamper_test")
        item = next(x for x in result["items"] if x["symbol"] == "EURUSD" and x["timeframe"] == "M5")
        self.assertEqual(item["status"], "FAILED_VALIDATION")
        self.assertEqual((self.root / "data/market/EURUSD/M5.csv").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

