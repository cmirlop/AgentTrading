"""Synthetic fixtures for offline backfill integration and security validation."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from src.history_data_integrity import CANONICAL_HASH_POLICY, content_hashes, validate_run_id
from src.market_history_audit import REQUIRED_COLUMNS, audit_market_history
from src.market_history_backfill import _source_hashes, execute_backfill_plan, plan_chunks, validate_backfill_run


UTC = timezone.utc
SYNTHETIC_SYMBOLS = ("EURUSD", "USDCAD")
SYNTHETIC_TIMEFRAMES = {"M5": 300, "M15": 900}
AS_OF = datetime(2026, 11, 11, tzinfo=UTC)
RECENT_ROOT = Path("data/market_history/mt5_real_readonly/20261109T202112.171333Z")


def synthetic_bar(symbol: str, timeframe: str, timestamp: datetime, *, spread: int | None = None) -> dict[str, Any]:
    seconds = SYNTHETIC_TIMEFRAMES[timeframe]
    symbol_base = {"EURUSD": 1.1, "USDCAD": 1.35}[symbol]
    marker = int(timestamp.timestamp()) // seconds % 10000
    open_price = symbol_base + marker * 0.0000001
    close_price = open_price + 0.00003
    return {
        "time": timestamp,
        "open": open_price,
        "high": close_price + 0.00002,
        "low": open_price - 0.00002,
        "close": close_price,
        "tick_volume": 100,
        "spread": 0 if spread is None and symbol == "EURUSD" else (2 if spread is None else spread),
        "real_volume": 0,
    }


def _row(bar: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": bar["time"].isoformat(),
        "open": f"{bar['open']:.8f}",
        "high": f"{bar['high']:.8f}",
        "low": f"{bar['low']:.8f}",
        "close": f"{bar['close']:.8f}",
        "tick_volume": str(bar["tick_volume"]),
        "spread": str(bar["spread"]),
        "real_volume": str(bar["real_volume"]),
    }


def _write_csv(path: Path, bars: list[dict[str, Any]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[dict[str, Any]] = [_row(bar) for bar in bars]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(lines)
    return path.read_bytes()


def create_synthetic_source_tree(root: Path) -> dict[str, Any]:
    """Create four small pair/timeframe datasets with two recoverable gaps."""
    snapshot = root / RECENT_ROOT
    manifest: dict[str, Any] = {"schema_version": 1, "symbols": {}}
    histories: dict[tuple[str, str], tuple[list[dict[str, Any]], list[dict[str, Any]]]] = {}

    large_gap_start = datetime(2025, 1, 6, 10, 0, tzinfo=UTC)
    large_gap_bars = 1002
    short_gap_start = datetime(2025, 1, 6, 13, 0, tzinfo=UTC)
    saturday = datetime(2025, 1, 4, 0, 0, tzinfo=UTC)
    regular = datetime(2025, 1, 6, 15, 0, tzinfo=UTC)

    histories[("EURUSD", "M5")] = (
        [synthetic_bar("EURUSD", "M5", large_gap_start - timedelta(minutes=10)),
         synthetic_bar("EURUSD", "M5", large_gap_start - timedelta(minutes=5))],
        [synthetic_bar("EURUSD", "M5", large_gap_start + timedelta(minutes=5 * large_gap_bars))],
    )
    histories[("EURUSD", "M15")] = (
        [synthetic_bar("EURUSD", "M15", saturday),
         synthetic_bar("EURUSD", "M15", saturday + timedelta(minutes=30))],
        [synthetic_bar("EURUSD", "M15", saturday + timedelta(minutes=30))],
    )
    histories[("USDCAD", "M5")] = (
        [synthetic_bar("USDCAD", "M5", short_gap_start - timedelta(minutes=10)),
         synthetic_bar("USDCAD", "M5", short_gap_start - timedelta(minutes=5))],
        [synthetic_bar("USDCAD", "M5", short_gap_start + timedelta(minutes=10))],
    )
    histories[("USDCAD", "M15")] = (
        [synthetic_bar("USDCAD", "M15", regular),
         synthetic_bar("USDCAD", "M15", regular + timedelta(minutes=15))],
        [synthetic_bar("USDCAD", "M15", regular + timedelta(minutes=15))],
    )

    for symbol in SYNTHETIC_SYMBOLS:
        manifest["symbols"][symbol] = {}
        for timeframe in SYNTHETIC_TIMEFRAMES:
            previous, recent = histories[(symbol, timeframe)]
            old_path = root / "data/market" / symbol / f"{timeframe}.csv"
            recent_path = snapshot / f"{symbol}_{timeframe}.csv"
            _write_csv(old_path, previous)
            recent_bytes = _write_csv(recent_path, recent)
            manifest["symbols"][symbol][timeframe] = {
                "sha256": hashlib.sha256(recent_bytes).hexdigest(),
                "new_bars": len(recent),
                "latest_closed_utc": max(bar["time"] for bar in recent).isoformat(),
            }
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {
        "symbols": SYNTHETIC_SYMBOLS,
        "timeframes_seconds": SYNTHETIC_TIMEFRAMES,
        "large_gap_start_utc": large_gap_start.isoformat(),
        "large_gap_missing_bars": large_gap_bars,
        "short_gap_start_utc": short_gap_start.isoformat(),
        "old_and_recent_source_rows": sum(len(old) + len(new) for old, new in histories.values()),
    }


class SyntheticMT5:
    """Minimal mocked MT5 API; it contains no terminal connection methods."""

    ACCOUNT_TRADE_MODE_REAL = 2
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15

    def __init__(
        self,
        *,
        fail_on_call: int | None = None,
        empty_first_chunk: bool = False,
        conflict_time: datetime | None = None,
    ):
        self.fail_on_call = fail_on_call
        self.empty_first_chunk = empty_first_chunk
        self.conflict_time = conflict_time
        self.tradeapi_disabled = True
        self.copy_count = 0
        self.calls: list[str] = []

    def terminal_info(self):
        self.calls.append("terminal_info")
        return {"connected": True, "tradeapi_disabled": self.tradeapi_disabled}

    def account_info(self):
        self.calls.append("account_info")
        return {"trade_mode": self.ACCOUNT_TRADE_MODE_REAL, "currency": "USD"}

    def copy_rates_range(self, symbol: str, timeframe_value: int, start: datetime, end: datetime):
        self.calls.append("copy_rates_range")
        self.copy_count += 1
        if self.fail_on_call == self.copy_count:
            raise RuntimeError("SIMULATED_INTERRUPTION")
        if self.empty_first_chunk and self.copy_count == 1:
            return []
        timeframe = {self.TIMEFRAME_M5: "M5", self.TIMEFRAME_M15: "M15"}[timeframe_value]
        seconds = SYNTHETIC_TIMEFRAMES[timeframe]
        records = []
        current = start
        while current <= end:
            bar = synthetic_bar(symbol, timeframe, current)
            if self.conflict_time == current:
                bar["close"] = bar["open"] + 0.0007
                bar["high"] = bar["close"] + 0.00002
            record = dict(bar)
            record["time"] = int(current.timestamp())
            records.append(record)
            current += timedelta(seconds=seconds)
        return records


def _new_plan(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture = create_synthetic_source_tree(root)
    gaps, _, plan = audit_market_history(
        root,
        as_of_utc=AS_OF,
        symbols=SYNTHETIC_SYMBOLS,
        timeframes_seconds=SYNTHETIC_TIMEFRAMES,
    )
    if gaps["status"] != "PASS_WITH_PENDING_GAPS" or plan["schema_version"] != 2:
        raise AssertionError("SYNTHETIC_INITIAL_AUDIT_FAILED")
    if gaps["history_continuity_status"] != "HISTORY_CONTINUITY_NOT_AUDITED":
        raise AssertionError("INITIAL_CONTINUITY_MUST_BE_NOT_AUDITED")
    return plan, fixture


def _audit_run(root: Path, run_id: str) -> dict[str, Any]:
    return audit_market_history(
        root,
        as_of_utc=AS_OF,
        backfill_run=run_id,
        symbols=SYNTHETIC_SYMBOLS,
        timeframes_seconds=SYNTHETIC_TIMEFRAMES,
    )[0]


def run_end_to_end_scenarios() -> dict[str, Any]:
    """Exercise full clean/resume, conflict, and empty-response cycles."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan, fixture = _new_plan(root)
        original_hashes = _source_hashes(root, symbols=SYNTHETIC_SYMBOLS, timeframes=SYNTHETIC_TIMEFRAMES)
        planned_chunk_count = len(plan_chunks(
            plan["backfill_intervals"],
            symbols=plan["symbols"],
            timeframes=plan["timeframes_seconds"],
            max_bars_per_request=plan["chunk_policy"]["max_bars_per_request"],
            overlap_bars_each_side=plan["chunk_policy"]["overlap_bars_each_side"],
        ))

        interrupt_api = SyntheticMT5(fail_on_call=2)
        interrupted = False
        try:
            execute_backfill_plan(interrupt_api, plan, root=root, run_id="synthetic-clean", as_of_utc=AS_OF)
        except RuntimeError as exc:
            interrupted = str(exc) == "SIMULATED_INTERRUPTION"
        first_chunk_files = list((root / "data/market_history/backfill_runs/synthetic-clean/chunks").glob("*.csv"))
        resume_api = SyntheticMT5()
        clean_manifest = execute_backfill_plan(resume_api, plan, root=root, run_id="synthetic-clean", as_of_utc=AS_OF)
        clean_audit = _audit_run(root, "synthetic-clean")
        clean_integrity = validate_backfill_run(root, "synthetic-clean", as_of_utc=AS_OF)
        after_clean_hashes = _source_hashes(root, symbols=SYNTHETIC_SYMBOLS, timeframes=SYNTHETIC_TIMEFRAMES)

        conflict_plan = plan
        # The conflict run uses the same frozen plan and alters only one returned overlap quote.
        conflict_at = datetime.fromisoformat(fixture["large_gap_start_utc"]) - timedelta(minutes=10)
        conflict_api = SyntheticMT5(conflict_time=conflict_at)
        conflict_manifest = execute_backfill_plan(conflict_api, conflict_plan, root=root, run_id="synthetic-conflict", as_of_utc=AS_OF)
        conflict_audit = _audit_run(root, "synthetic-conflict")

        empty_api = SyntheticMT5(empty_first_chunk=True)
        empty_manifest = execute_backfill_plan(empty_api, plan, root=root, run_id="synthetic-empty", as_of_utc=AS_OF)
        partial_audit = _audit_run(root, "synthetic-empty")
        after_all_hashes = _source_hashes(root, symbols=SYNTHETIC_SYMBOLS, timeframes=SYNTHETIC_TIMEFRAMES)

        return {
            "data_provenance": "SYNTHETIC_TEST_DATA_NOT_REAL_RECOVERY",
            "real_mt5_used": False,
            "llm_calls": 0,
            "orders_sent": 0,
            "initial_audit": {
                "status": "PASS_WITH_PENDING_GAPS",
                "history_continuity_status": "HISTORY_CONTINUITY_NOT_AUDITED",
                "planned_intervals": planned_chunk_count,
                "large_gap_missing_bars": fixture["large_gap_missing_bars"],
            },
            "interruption_and_resume": {
                "interruption_observed": interrupted,
                "first_chunk_files_after_interruption": len(first_chunk_files),
                "retry_provider_calls": resume_api.copy_count,
                "first_completed_chunk_not_refetched": resume_api.copy_count == planned_chunk_count - 1,
                "manifest_cumulative_provider_calls": clean_manifest["provider_calls"],
            },
            "clean_recovery": {
                "backfill_manifest_status": clean_manifest["status"],
                "backfill_integrity_status": clean_integrity["integrity_status"],
                "continuity_status": clean_audit["history_continuity_status"],
                "remaining_recovery_intervals": clean_audit["counts"]["pending_recovery_intervals"],
                "calendar_review_intervals": clean_audit["counts"]["calendar_review_intervals"],
                "valid_chunks": clean_integrity["accepted_chunk_count"],
                "originals_unchanged": original_hashes == after_clean_hashes,
            },
            "conflict_recovery": {
                "backfill_manifest_status": conflict_manifest["status"],
                "continuity_status": conflict_audit["history_continuity_status"],
                "conflict_status": conflict_audit.get("backfill_run", {}).get("integrity_status"),
                "conflict_count": conflict_audit["counts"]["backfill_source_conflicts"],
                "conflict_action": (
                    conflict_audit.get("backfill_run", {}).get("source_conflicts", [{}])[0].get("status")
                    if conflict_audit.get("backfill_run", {}).get("source_conflicts") else None
                ),
            },
            "empty_response_recovery": {
                "backfill_manifest_status": empty_manifest["status"],
                "continuity_status": partial_audit["history_continuity_status"],
                "empty_chunks": len(empty_manifest.get("chunks", {})) and sum(bool(item.get("empty_response")) for item in empty_manifest["chunks"].values()),
                "pending_chunk_count": len(_audit_run_validation(root, "synthetic-empty")["pending_chunk_ids"]),
            },
            "original_sources_unchanged_after_all_scenarios": original_hashes == after_all_hashes,
            "original_source_file_count": len(original_hashes),
            "mock_market_read_calls": interrupt_api.copy_count + resume_api.copy_count + conflict_api.copy_count + empty_api.copy_count,
            "post_backfill_audit": {
                "data_provenance": "SYNTHETIC_TEST_DATA_NOT_REAL_RECOVERY",
                "real_download_performed": False,
                "history_continuity_status": clean_audit["history_continuity_status"],
                "audit": clean_audit,
            },
        }


def _audit_run_validation(root: Path, run_id: str) -> dict[str, Any]:
    return validate_backfill_run(root, run_id, as_of_utc=AS_OF)


def run_portability_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan, _ = _new_plan(root)
        plan_hash = plan["source_file_hashes"]
        for path in list((root / "data/market").rglob("*.csv")) + list((root / "data/market_history").rglob("*.csv")):
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        actual = _source_hashes(root, symbols=SYNTHETIC_SYMBOLS, timeframes=SYNTHETIC_TIMEFRAMES)
        api = SyntheticMT5()
        manifest = execute_backfill_plan(api, plan, root=root, run_id="portable-plan", as_of_utc=AS_OF)
        paths = root / "data/market_history/backfill_runs/portable-plan/chunks"
        changed_chunk_hash = False
        if paths.exists():
            for path in paths.glob("*.csv"):
                path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
                changed_chunk_hash = True
                break
        run_validation = validate_backfill_run(root, "portable-plan", as_of_utc=AS_OF)
        return {
            "data_provenance": "SYNTHETIC_TEST_DATA_NOT_REAL_RECOVERY",
            "policy": CANONICAL_HASH_POLICY,
            "plan_generated_with_lf_source_files": True,
            "source_raw_hashes_differ_after_crlf_conversion": any(plan_hash[key]["raw_sha256"] != actual[key]["raw_sha256"] for key in plan_hash),
            "source_canonical_hashes_equal_after_crlf_conversion": all(plan_hash[key]["canonical_sha256"] == actual[key]["canonical_sha256"] for key in plan_hash),
            "plan_accepted_on_crlf_sources": manifest["status"] == "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT",
            "chunk_line_endings_changed_after_write": changed_chunk_hash,
            "auditor_accepts_canonical_chunk_hash": run_validation["integrity_status"] == "PASS",
            "audited_chunk_hash_statuses": [item.get("hash_status") for item in run_validation["chunks"] if item.get("accepted")],
        }


def run_path_safety_scenario() -> dict[str, Any]:
    accepted = validate_run_id("backfill-20261109")
    rejected = [".", "..", "../other", "..\\other", "C:\\temp", "CON", "NUL", "COM1"]
    rejection_results = {}
    for value in rejected:
        try:
            validate_run_id(value)
        except ValueError:
            rejection_results[value] = True
        else:
            rejection_results[value] = False
    symlink_rejected = False
    no_external_write = False
    with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
        root = Path(directory)
        runs = root / "data/market_history/backfill_runs"
        runs.mkdir(parents=True)
        try:
            (runs / "link-run").symlink_to(Path(outside), target_is_directory=True)
            try:
                from src.history_data_integrity import backfill_run_paths

                backfill_run_paths(root, "link-run", create=True)
            except ValueError as exc:
                symlink_rejected = str(exc) == "BACKFILL_SYMLINK_PATH_REJECTED"
        except OSError:
            symlink_rejected = False
        no_external_write = list(Path(outside).iterdir()) == []
    return {
        "valid_run_id": accepted,
        "invalid_run_ids_rejected": rejection_results,
        "all_invalid_run_ids_rejected": all(rejection_results.values()),
        "symlink_path_rejected": symlink_rejected,
        "no_external_write": no_external_write,
    }
