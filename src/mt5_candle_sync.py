"""Auditable, read-only synchronization of provider-observed MT5 candles."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Callable

from config.market_config import SYMBOLS, TIMEFRAMES
from src.history_data_integrity import (
    ensure_no_symlink_path,
    file_content_hashes,
    safe_atomic_write_bytes,
    validate_run_id,
)
from src.market_history_audit import REQUIRED_COLUMNS, TIMEFRAMES_SECONDS
from src.market_history_backfill import (
    MAX_BARS_PER_REQUEST,
    _record_to_mapping,
    safe_copy_rates_from_pos,
    verify_real_readonly_account,
)
from scripts.validate_mt5_real_readonly import (
    READ_ONLY_METHODS,
    SafetyGateError,
    _attr,
    _read,
    _verify_terminal_gate,
)


MAX_BATCH_CALLS = 150
MAX_TOTAL_CALLS = 1500
DEFAULT_PAGE_SIZE = 1000
SCHEMA_VERSION = 1
FROZEN_DATASET = "data/backtest/technical_candidate_scan.csv"
FROZEN_DATASET_CANONICAL_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
TIMEFRAME_API_CONSTANTS = {
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}
FORBIDDEN_METHODS = frozenset({"order_send", "order_check", "login", "symbol_select"})


class CandleSyncError(RuntimeError):
    """Safe failure for candle synchronization."""


def _utc(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, (int, float)):
        value = datetime.fromtimestamp(int(raw), timezone.utc)
    else:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMESTAMP_TIMEZONE_MISSING")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("TIMESTAMP_NOT_UTC")
    return value.astimezone(timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _atomic_json(path: Path, value: Any, *, root: Path) -> None:
    safe_atomic_write_bytes(path, _json_bytes(value), authorized_root=root, overwrite=True)


def _float_text(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("NUMBER_NONFINITE")
    return format(number, ".15g")


def _signature(row: dict[str, Any]) -> tuple[str, ...]:
    return (
        *(_float_text(row[key]) for key in ("open", "high", "low", "close")),
        _float_text(row["tick_volume"]),
        str(int(row["spread"])),
        _float_text(row["real_volume"]),
    )


def _normalize_rate(raw: Any) -> dict[str, Any]:
    source = _record_to_mapping(raw)
    timestamp = _utc(source["time"])
    numbers: dict[str, float] = {}
    for key in ("open", "high", "low", "close", "tick_volume", "real_volume"):
        value = float(source[key])
        if not math.isfinite(value):
            raise ValueError("BAR_NONFINITE_FIELD")
        numbers[key] = value
    if min(numbers[name] for name in ("open", "high", "low", "close")) <= 0:
        raise ValueError("BAR_NONPOSITIVE_PRICE")
    if numbers["low"] > min(numbers["open"], numbers["close"]):
        raise ValueError("BAR_LOW_ABOVE_BODY")
    if numbers["high"] < max(numbers["open"], numbers["close"]):
        raise ValueError("BAR_HIGH_BELOW_BODY")
    if numbers["low"] > numbers["high"]:
        raise ValueError("BAR_LOW_ABOVE_HIGH")
    if numbers["tick_volume"] < 0 or numbers["real_volume"] < 0:
        raise ValueError("BAR_NEGATIVE_VOLUME")
    spread_value = float(source["spread"])
    spread = int(spread_value)
    if spread < 0 or spread_value != spread:
        raise ValueError("BAR_NEGATIVE_SPREAD")
    return {"time": timestamp, **numbers, "spread": spread}


def _bar_json(bar: dict[str, Any]) -> dict[str, Any]:
    return {key: (value.isoformat() if key == "time" else value) for key, value in bar.items()}


def _validate_local(path: Path, timeframe: str) -> dict[str, Any]:
    raw = path.read_bytes()
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if not all(name in fields for name in REQUIRED_COLUMNS):
        raise CandleSyncError("LOCAL_CSV_REQUIRED_COLUMNS_MISSING")
    seen: dict[datetime, tuple[str, ...]] = {}
    parsed: list[tuple[datetime, dict[str, str]]] = []
    invalid = 0
    conflicts: list[dict[str, Any]] = []
    prior_time: datetime | None = None
    out_of_order = 0
    for row in rows:
        try:
            timestamp = _utc(row["time"])
            candidate = {
                "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"],
                "tick_volume": row["tick_volume"], "spread": row["spread"], "real_volume": row["real_volume"],
            }
            _signature(candidate)
            numeric = {key: float(candidate[key]) for key in ("open", "high", "low", "close")}
            if min(numeric.values()) <= 0 or numeric["low"] > min(numeric["open"], numeric["close"]) or numeric["high"] < max(numeric["open"], numeric["close"]) or numeric["low"] > numeric["high"]:
                raise ValueError("LOCAL_OHLC_INVALID")
            if float(row["tick_volume"]) < 0 or float(row["real_volume"]) < 0 or int(row["spread"]) < 0:
                raise ValueError("LOCAL_VOLUME_OR_SPREAD_INVALID")
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        if prior_time is not None and timestamp <= prior_time:
            out_of_order += 1
        prior_time = timestamp
        signature = _signature(candidate)
        if timestamp in seen:
            conflicts.append({"time_utc": timestamp.isoformat(), "kind": "LOCAL_DUPLICATE", "identical": seen[timestamp] == signature})
        else:
            seen[timestamp] = signature
        parsed.append((timestamp, row))
    if invalid or out_of_order or conflicts:
        raise CandleSyncError("LOCAL_CSV_INTEGRITY_REQUIRES_REVIEW")
    parsed.sort(key=lambda item: item[0])
    return {
        "path": path,
        "raw": raw,
        "raw_sha256": _sha256(raw),
        "canonical_sha256": _sha256(raw.replace(b"\r\n", b"\n")),
        "fields": fields,
        "rows": parsed,
        "first_utc": parsed[0][0].isoformat() if parsed else None,
        "last_utc": parsed[-1][0].isoformat() if parsed else None,
        "row_count": len(rows),
        "invalid_rows": invalid,
        "out_of_order_rows": out_of_order,
        "duplicate_rows": len(conflicts),
        "timeframe": timeframe,
    }


def _pair_id(symbol: str, timeframe: str) -> str:
    return f"{symbol}|{timeframe}"


def _pair_path(run_root: Path, symbol: str, timeframe: str, page_index: int) -> Path:
    return run_root / "raw_chunks" / f"{symbol}_{timeframe}_{page_index:06d}.json"


def _base_timeframe_report(symbol: str, timeframe: str, local: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": "PENDING",
        "provider_first_utc": None,
        "provider_last_closed_utc": None,
        "provider_oldest_boundary_verified": False,
        "local_first_before_utc": local.get("first_utc") if local else None,
        "local_last_before_utc": local.get("last_utc") if local else None,
        "local_first_after_utc": local.get("first_utc") if local else None,
        "local_last_after_utc": local.get("last_utc") if local else None,
        "rows_before": local.get("row_count", 0) if local else 0,
        "rows_after": local.get("row_count", 0) if local else 0,
        "new_rows": 0,
        "new_rows_older_than_local": 0,
        "new_rows_inside_local_range": 0,
        "new_rows_newer_than_local": 0,
        "exact_duplicates_collapsed": 0,
        "source_conflicts": 0,
        "unconfirmed_gaps_after": 0,
        "provider_calls": 0,
        "last_error_code": None,
        "open_or_future_rows_excluded": 0,
        "spread_zero_rows": 0,
        "raw_sha256": None,
        "canonical_sha256": None,
    }


def _read_symbol_info(api: Any, symbol: str) -> Any:
    verify_real_readonly_account(api)
    try:
        info = _read(api, "symbol_info", symbol)
    except Exception:
        verify_real_readonly_account(api)
        raise
    verify_real_readonly_account(api)
    return info


def _read_last_error(api: Any) -> dict[str, Any]:
    if "last_error" not in READ_ONLY_METHODS:
        return {"code": None, "message": "LAST_ERROR_NOT_ALLOWLISTED"}
    verify_real_readonly_account(api)
    result = _read(api, "last_error")
    verify_real_readonly_account(api)
    if isinstance(result, (tuple, list)) and len(result) >= 2:
        return {"code": result[0], "message": str(result[1])}
    return {"code": None, "message": "LAST_ERROR_FORMAT_UNRECOGNIZED"}


def _safe_symbol_info(info: Any) -> dict[str, Any]:
    if info is None:
        return {"available": False, "visible": None}
    visible = _attr(info, "visible")
    return {"available": True, "visible": visible is True}


def _inventory_and_backup(root: Path, run_id: str) -> dict[str, Any]:
    validate_run_id(run_id)
    private_root = ensure_no_symlink_path(root.resolve(), root / "reports" / "private" / "phase2_2_2f")
    private_root.mkdir(parents=True, exist_ok=True)
    run_root = ensure_no_symlink_path(root.resolve(), private_root / run_id)
    run_root.mkdir(exist_ok=True)
    inventory_path = run_root / "pre_sync_inventory.json"
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if "source_csv_hashes" not in inventory:
            inventory["source_csv_hashes"] = {
                item["path"]: item["raw_sha256"] for item in inventory.get("files", [])
            }
        for snapshot in inventory.get("prior_snapshots", []):
            if "hashes" not in snapshot and isinstance(snapshot.get("raw_hashes"), dict):
                snapshot["hashes"] = snapshot["raw_hashes"]
        prior = inventory.get("prior_private_run", {})
        if prior and "path" not in prior and prior.get("run_id"):
            prior["path"] = f"data/market_history/backfill_runs/{prior['run_id']}"
        if prior and "chunk_hashes" not in prior and prior.get("path"):
            prior_manifest_path = root / prior["path"] / "manifest.json"
            if prior_manifest_path.is_file():
                try:
                    prior_manifest = json.loads(prior_manifest_path.read_text(encoding="utf-8"))
                    prior["chunk_hashes"] = {
                        Path(entry.get("file", f"{chunk_id}.csv")).name: entry["raw_sha256"]
                        for chunk_id, entry in prior_manifest.get("chunks", {}).items()
                        if entry.get("raw_sha256")
                    }
                except (OSError, json.JSONDecodeError, TypeError):
                    prior["chunk_hashes"] = {}
        _atomic_json(inventory_path, inventory, root=root)
        recorded_after_sync: dict[str, str] = {}
        manifest_path = run_root / "manifest.json"
        if manifest_path.is_file():
            try:
                prior_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                recorded_after_sync = prior_manifest.get("csv_hashes_after_sync", {})
            except (OSError, json.JSONDecodeError):
                recorded_after_sync = {}
        for item in inventory.get("files", []):
            source = root / item["path"]
            backup = run_root / "source_csv_backup" / f"{item['symbol']}_{item['timeframe']}.csv"
            expected_current = recorded_after_sync.get(_pair_id(item["symbol"], item["timeframe"]), item["raw_sha256"])
            if not source.is_file() or not backup.is_file() or _sha256(source.read_bytes()) != expected_current or _sha256(backup.read_bytes()) != item["raw_sha256"]:
                raise CandleSyncError("PRE_SYNC_BACKUP_OR_SOURCE_HASH_MISMATCH")
        if len(inventory.get("files", [])) != len(SYMBOLS) * len(TIMEFRAMES):
            raise CandleSyncError("PRE_SYNC_INVENTORY_INCOMPLETE")
        actual_frozen = file_content_hashes(root / FROZEN_DATASET)
        if actual_frozen["canonical_sha256"] != FROZEN_DATASET_CANONICAL_SHA256:
            raise CandleSyncError("FROZEN_DATASET_HASH_MISMATCH")
        return inventory

    backup_root = run_root / "source_csv_backup"
    backup_root.mkdir(exist_ok=False)
    files: list[dict[str, Any]] = []
    source_hashes: dict[str, str] = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            path = root / "data" / "market" / symbol / f"{timeframe}.csv"
            local = _validate_local(path, timeframe)
            backup = backup_root / f"{symbol}_{timeframe}.csv"
            shutil.copy2(path, backup)
            backup_sha = _sha256(backup.read_bytes())
            if backup_sha != local["raw_sha256"]:
                raise CandleSyncError("PRE_SYNC_BACKUP_HASH_MISMATCH")
            relative = path.relative_to(root).as_posix()
            source_hashes[relative] = local["raw_sha256"]
            files.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "path": relative,
                "columns": local["fields"],
                "row_count": local["row_count"],
                "first_utc": local["first_utc"],
                "last_utc": local["last_utc"],
                "raw_sha256": local["raw_sha256"],
                "canonical_sha256": local["canonical_sha256"],
                "backup_sha256": backup_sha,
                "duplicate_rows": local["duplicate_rows"],
                "invalid_rows": local["invalid_rows"],
                "out_of_order_rows": local["out_of_order_rows"],
            })
    frozen = root / FROZEN_DATASET
    frozen_hashes = file_content_hashes(frozen)
    snapshots: list[dict[str, Any]] = []
    snapshot_root = root / "data" / "market_history" / "mt5_real_readonly"
    if snapshot_root.exists():
        for directory in sorted(path for path in snapshot_root.iterdir() if path.is_dir()):
            snap_files = sorted(directory.glob("*.csv"))
            snapshots.append({
                "run": directory.name,
                "csv_count": len(snap_files),
                "hashes": {path.name: _sha256(path.read_bytes()) for path in snap_files},
                "manifest_sha256": _sha256((directory / "manifest.json").read_bytes()) if (directory / "manifest.json").is_file() else None,
            })
    old_run = root / "data" / "market_history" / "backfill_runs" / "phase222b-local-20261115"
    old_manifest = old_run / "manifest.json"
    old_chunks = sorted((old_run / "chunks").glob("*.csv")) if (old_run / "chunks").is_dir() else []
    prior_run = {
        "path": old_run.relative_to(root).as_posix(),
        "manifest_sha256": _sha256(old_manifest.read_bytes()) if old_manifest.is_file() else None,
        "chunk_count": len(old_chunks),
        "chunk_hashes": {path.name: _sha256(path.read_bytes()) for path in old_chunks},
    }
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "backup_dir": backup_root.relative_to(root).as_posix(),
        "files": files,
        "source_csv_hashes": source_hashes,
        "frozen_dataset": {
            "path": FROZEN_DATASET,
            **frozen_hashes,
            "expected_canonical_sha256": FROZEN_DATASET_CANONICAL_SHA256,
            "unchanged_expected": frozen_hashes["canonical_sha256"] == FROZEN_DATASET_CANONICAL_SHA256,
        },
        "prior_snapshots": snapshots,
        "prior_private_run": prior_run,
    }
    _atomic_json(inventory_path, inventory, root=root)
    if not inventory["frozen_dataset"]["unchanged_expected"]:
        raise CandleSyncError("FROZEN_DATASET_HASH_MISMATCH")
    return inventory


def _verify_preserved_inputs(root: Path, inventory: dict[str, Any]) -> None:
    frozen = file_content_hashes(root / FROZEN_DATASET)
    expected_frozen = inventory.get("frozen_dataset", {}).get("canonical_sha256")
    if frozen.get("canonical_sha256") != FROZEN_DATASET_CANONICAL_SHA256 or expected_frozen != frozen.get("canonical_sha256"):
        raise CandleSyncError("FROZEN_DATASET_CHANGED_DURING_CANDLE_SYNC")
    for snapshot in inventory.get("prior_snapshots", []):
        directory = root / "data" / "market_history" / "mt5_real_readonly" / snapshot["run"]
        actual_files = {path.name: _sha256(path.read_bytes()) for path in directory.glob("*.csv")}
        if actual_files != snapshot.get("hashes", {}):
            raise CandleSyncError("PRIOR_MT5_SNAPSHOT_CHANGED")
        manifest_path = directory / "manifest.json"
        actual_manifest = _sha256(manifest_path.read_bytes()) if manifest_path.is_file() else None
        if actual_manifest != snapshot.get("manifest_sha256"):
            raise CandleSyncError("PRIOR_MT5_SNAPSHOT_MANIFEST_CHANGED")
    prior = inventory.get("prior_private_run", {})
    if prior.get("manifest_sha256"):
        prior_root = root / prior["path"]
        if _sha256((prior_root / "manifest.json").read_bytes()) != prior["manifest_sha256"]:
            raise CandleSyncError("PRIOR_BACKFILL_MANIFEST_CHANGED")
        actual_chunks = {path.name: _sha256(path.read_bytes()) for path in (prior_root / "chunks").glob("*.csv")}
        if actual_chunks != prior.get("chunk_hashes", {}):
            raise CandleSyncError("PRIOR_BACKFILL_CHUNKS_CHANGED")


def _new_manifest(root: Path, run_id: str, as_of_utc: datetime, inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "as_of_utc": as_of_utc.astimezone(timezone.utc).isoformat(),
        "account_type": "UNKNOWN",
        "tradeapi_disabled_verified": False,
        "source_csv_hashes": inventory["source_csv_hashes"],
        "frozen_dataset_canonical_sha256": inventory["frozen_dataset"]["canonical_sha256"],
        "provider_call_limit_total": MAX_TOTAL_CALLS,
        "provider_calls": 0,
        "orders_sent": 0,
        "trades_executed": 0,
        "mt5_operations": [],
        "symbol_info": {},
        "pairs": {
            _pair_id(symbol, timeframe): {
                "symbol": symbol,
                "timeframe": timeframe,
                "status": "PENDING",
                "next_position": 0,
                "page_count": 0,
                "last_page_oldest_two_utc": [],
                "provider_first_utc": None,
                "provider_latest_utc": None,
                "provider_oldest_boundary_verified": False,
                "open_or_future_rows_excluded": 0,
                "provider_conflicts": [],
                "provider_exact_duplicates": 0,
                "invalid_rows": 0,
                "error_code": None,
                "pages": [],
            }
            for symbol in SYMBOLS for timeframe in TIMEFRAMES
        },
        "status": "AUDIT_RUNNING",
    }


def _load_manifest(run_root: Path, run_id: str, inventory: dict[str, Any]) -> dict[str, Any]:
    path = run_root / "manifest.json"
    if not path.is_file():
        raise CandleSyncError("RUN_MANIFEST_NOT_FOUND")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("run_id") != run_id:
        raise CandleSyncError("RUN_MANIFEST_IDENTITY_INVALID")
    if manifest.get("source_csv_hashes") != inventory.get("source_csv_hashes"):
        raise CandleSyncError("SOURCE_CSV_HASHES_CHANGED_SINCE_INVENTORY")
    if manifest.get("frozen_dataset_canonical_sha256") != FROZEN_DATASET_CANONICAL_SHA256:
        raise CandleSyncError("FROZEN_DATASET_PROVENANCE_MISMATCH")
    return manifest


def _safe_error(api: Any) -> dict[str, Any]:
    try:
        error = _read_last_error(api)
        return {"code": error.get("code"), "message": error.get("message")}
    except SafetyGateError:
        raise
    except Exception as exc:
        return {"code": None, "message": type(exc).__name__}


def _page_bars(rates: Any, timeframe: str, as_of_utc: datetime) -> tuple[list[dict[str, Any]], int, int, int, list[dict[str, Any]]]:
    bars: list[dict[str, Any]] = []
    invalid = 0
    excluded_open = 0
    exact_duplicates = 0
    conflicts: list[dict[str, Any]] = []
    by_time: dict[datetime, dict[str, Any]] = {}
    closure_seconds = TIMEFRAMES_SECONDS[timeframe] + (3600 if timeframe == "D1" else 0)
    for raw in rates:
        try:
            bar = _normalize_rate(raw)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
            continue
        if bar["time"] + timedelta(seconds=closure_seconds) > as_of_utc:
            excluded_open += 1
        signature = _signature(bar)
        previous = by_time.get(bar["time"])
        if previous is not None:
            if _signature(previous) != signature:
                conflicts.append({"time_utc": bar["time"].isoformat(), "kind": "PROVIDER_DUPLICATE_CONFLICT"})
            else:
                exact_duplicates += 1
            continue
        by_time[bar["time"]] = bar
    bars = [by_time[timestamp] for timestamp in sorted(by_time)]
    return bars, invalid, excluded_open, exact_duplicates, conflicts


def _persist_page(root: Path, run_root: Path, pair: dict[str, Any], position: int, requested: int, rates: Any, error: dict[str, Any] | None = None) -> dict[str, Any]:
    page_index = int(pair["page_count"])
    path = _pair_path(run_root, pair["symbol"], pair["timeframe"], page_index)
    ensure_no_symlink_path(root.resolve(), path)
    rows: list[dict[str, Any]] = []
    if rates is not None:
        for record in rates:
            source = _record_to_mapping(record)
            clean: dict[str, Any] = {}
            for name in ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"):
                value = source[name]
                clean[name] = value.item() if hasattr(value, "item") else value
            clean["time"] = int(clean["time"])
            rows.append(clean)
    payload = _json_bytes({"symbol": pair["symbol"], "timeframe": pair["timeframe"], "position": position, "requested": requested, "rows": rows})
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_atomic_write_bytes(path, payload, authorized_root=run_root, overwrite=False)
    return {
        "page_index": page_index,
        "position": position,
        "requested": requested,
        "returned": len(rows),
        "path": path.relative_to(root).as_posix(),
        "raw_sha256": _sha256(payload),
        "oldest_utc": rows[0]["time"] if rows else None,
        "newest_utc": rows[-1]["time"] if rows else None,
        "error": error,
    }


def audit_provider(
    api: Any,
    *,
    root: Path,
    run_id: str,
    as_of_utc: datetime | None = None,
    max_provider_calls: int = MAX_BATCH_CALLS,
    page_size: int = DEFAULT_PAGE_SIZE,
    progress: Callable[[dict[str, Any]], None] | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Fetch provider-observed bars in positional pages; never writes public CSVs."""
    if not 1 <= max_provider_calls <= MAX_BATCH_CALLS:
        raise ValueError("BATCH_PROVIDER_CALL_LIMIT_INVALID")
    if not 1 <= page_size <= MAX_BARS_PER_REQUEST:
        raise ValueError("PAGE_SIZE_INVALID")
    if "copy_rates_from_pos" not in READ_ONLY_METHODS or FORBIDDEN_METHODS & READ_ONLY_METHODS:
        raise SafetyGateError("MT5_READ_ALLOWLIST_UNSAFE")
    validate_run_id(run_id)
    run_root = root / "reports" / "private" / "phase2_2_2f" / run_id
    inventory = _inventory_and_backup(root, run_id)
    _verify_preserved_inputs(root, inventory)
    if resume:
        manifest = _load_manifest(run_root, run_id, inventory)
        as_of = _utc(manifest["as_of_utc"])
    else:
        as_of = (as_of_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if as_of_utc is not None and (as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None):
            raise ValueError("AS_OF_UTC_MUST_BE_AWARE")
        if (run_root / "manifest.json").exists():
            raise CandleSyncError("RUN_ID_ALREADY_EXISTS_USE_RESUME")
        manifest = _new_manifest(root, run_id, as_of, inventory)
        _atomic_json(run_root / "manifest.json", manifest, root=root)

    if not api.initialize():
        manifest["status"] = "BLOCKED_SAFETY"
        manifest["error_code"] = "MT5_INITIALIZE_FAILED"
        manifest["tradeapi_disabled_verified"] = False
        _atomic_json(run_root / "manifest.json", manifest, root=root)
        _verify_preserved_inputs(root, inventory)
        return manifest

    try:
        manifest["read_only_safety"] = verify_real_readonly_account(api)
        if manifest["read_only_safety"].get("account_type") != "REAL":
            raise SafetyGateError("ACCOUNT_NOT_VERIFIED_REAL")
        terminal_view = api.terminal_info()
        if _attr(terminal_view, "tradeapi_disabled") is not True or _attr(terminal_view, "connected") is not True:
            raise SafetyGateError("TERMINAL_READ_ONLY_GATE_LOST")
        maxbars = _attr(terminal_view, "maxbars")
        manifest["terminal_maxbars_observed"] = int(maxbars) if isinstance(maxbars, (int, float)) and maxbars > 0 else None
        _verify_terminal_gate(api)
        manifest["account_type"] = "REAL"
        manifest["tradeapi_disabled_verified"] = True
        for symbol in SYMBOLS:
            if symbol not in manifest["symbol_info"]:
                try:
                    info = _read_symbol_info(api, symbol)
                    safe = _safe_symbol_info(info)
                    if not safe["available"]:
                        safe["last_error"] = _safe_error(api)
                except SafetyGateError:
                    raise
                except Exception as exc:
                    safe = {"available": False, "visible": None, "error_type": type(exc).__name__, "last_error": _safe_error(api)}
                manifest["symbol_info"][symbol] = safe
                _atomic_json(run_root / "manifest.json", manifest, root=root)

        calls_this_batch = 0
        api_constants = {name: getattr(api, const, None) for name, const in TIMEFRAME_API_CONSTANTS.items()}
        if any(value is None for value in api_constants.values()):
            raise CandleSyncError("MT5_TIMEFRAME_CONSTANT_UNAVAILABLE")
        pair_ids = list(manifest["pairs"])
        while calls_this_batch < max_provider_calls:
            pending_ids = [key for key in pair_ids if manifest["pairs"][key]["status"] in {"PENDING", "IN_PROGRESS"}]
            if not pending_ids:
                break
            progress_made = False
            for key in pending_ids:
                if calls_this_batch >= max_provider_calls:
                    break
                pair = manifest["pairs"][key]
                if manifest["symbol_info"].get(pair["symbol"], {}).get("available") is not True or manifest["symbol_info"].get(pair["symbol"], {}).get("visible") is not True:
                    pair["status"] = "BLOCKED_SYMBOL_NOT_VISIBLE"
                    pair["error_code"] = "SYMBOL_INFO_UNAVAILABLE_OR_NOT_VISIBLE"
                    _atomic_json(run_root / "manifest.json", manifest, root=root)
                    continue
                position = int(pair["next_position"])
                requested = page_size
                if manifest["provider_calls"] >= MAX_TOTAL_CALLS:
                    manifest["status"] = "PARTIAL_PROVIDER_CALL_BUDGET"
                    break
                try:
                    rates = safe_copy_rates_from_pos(api, pair["symbol"], api_constants[pair["timeframe"]], position, requested)
                except SafetyGateError:
                    raise
                except Exception as exc:
                    error = _safe_error(api)
                    page_entry = _persist_page(root, run_root, pair, position, requested, None, error)
                    pair["pages"].append(page_entry)
                    pair["page_count"] += 1
                    pair["provider_calls"] = int(pair.get("provider_calls", 0)) + 1
                    pair["last_error_code"] = error.get("code")
                    pair["status"] = "PARTIAL_PROVIDER_LIMIT_UNKNOWN"
                    pair["error_type"] = type(exc).__name__
                    manifest["provider_calls"] += 1
                    calls_this_batch += 1
                    progress_made = True
                    if progress:
                        progress({"event": "READ_FAILED", "symbol": pair["symbol"], "timeframe": pair["timeframe"], "position": position, "error_code": pair["last_error_code"]})
                    _atomic_json(run_root / "manifest.json", manifest, root=root)
                    continue

                # Count each actual rates request, including an empty response.
                manifest["provider_calls"] += 1
                pair["provider_calls"] = int(pair.get("provider_calls", 0)) + 1
                calls_this_batch += 1
                progress_made = True
                if rates is None:
                    error = _safe_error(api)
                    page_entry = _persist_page(root, run_root, pair, position, requested, None, error)
                    pair["pages"].append(page_entry)
                    pair["page_count"] += 1
                    pair["last_error_code"] = error.get("code")
                    pair["status"] = "PARTIAL_PROVIDER_LIMIT_UNKNOWN"
                    _atomic_json(run_root / "manifest.json", manifest, root=root)
                    continue

                raw_count = len(rates)
                page_entry = _persist_page(root, run_root, pair, position, requested, rates)
                pair["pages"].append(page_entry)
                pair["page_count"] += 1
                try:
                    normalized, invalid_count, open_count, exact_duplicates, internal_conflicts = _page_bars(rates, pair["timeframe"], as_of)
                except Exception as exc:
                    pair["status"] = "FAILED_VALIDATION"
                    pair["error_code"] = type(exc).__name__
                    _atomic_json(run_root / "manifest.json", manifest, root=root)
                    continue
                pair["invalid_rows"] += invalid_count
                pair["open_or_future_rows_excluded"] += open_count
                pair["provider_exact_duplicates"] += exact_duplicates
                pair["provider_conflicts"].extend(internal_conflicts)
                page_times = [bar["time"] for bar in normalized]
                if page_times:
                    pair["provider_first_utc"] = min(pair["provider_first_utc"], page_times[0].isoformat()) if pair["provider_first_utc"] else page_times[0].isoformat()
                    closed_latest = [bar["time"] for bar in normalized if bar["time"] + timedelta(seconds=TIMEFRAMES_SECONDS[pair["timeframe"]] + (3600 if pair["timeframe"] == "D1" else 0)) <= as_of]
                    if closed_latest:
                        latest_closed = max(closed_latest).isoformat()
                        pair["provider_latest_utc"] = max(pair["provider_latest_utc"], latest_closed) if pair["provider_latest_utc"] else latest_closed

                if invalid_count:
                    pair["status"] = "FAILED_VALIDATION"
                    pair["error_code"] = "PROVIDER_BAR_INVALID"
                elif internal_conflicts:
                    pair["status"] = "PARTIAL_SOURCE_CONFLICT"
                elif raw_count == 0:
                    if pair["page_count"] <= 1:
                        pair["status"] = "PARTIAL_MT5_HISTORY_LIMIT"
                        pair["error_code"] = "NO_PROVIDER_BARS_RETURNED"
                    else:
                        pair["status"] = "COMPLETE_VS_MT5_OBSERVED"
                        pair["provider_oldest_boundary_verified"] = True
                elif raw_count < requested:
                    pair["status"] = "COMPLETE_VS_MT5_OBSERVED"
                    pair["provider_oldest_boundary_verified"] = True
                else:
                    if position > 0:
                        expected_overlap = set(pair.get("last_page_oldest_two_utc", []))
                        actual = {bar["time"].isoformat(): _signature(bar) for bar in normalized}
                        matched = expected_overlap & set(actual)
                        if len(matched) < min(2, len(expected_overlap)):
                            pair["status"] = "PARTIAL_SOURCE_CONFLICT"
                            pair["error_code"] = "POSITIONAL_PAGE_OVERLAP_MISSING"
                        else:
                            # Compare every overlap row against prior immutable chunks.
                            prior = _load_pair_bars(root, run_root, pair, page_limit=pair["page_count"] - 1)
                            for bar in normalized:
                                previous = prior.get(bar["time"])
                                if previous and _signature(previous) != _signature(bar):
                                    pair["provider_conflicts"].append({"time_utc": bar["time"].isoformat(), "kind": "CHUNK_OVERLAP_CONTENT_CHANGED"})
                                elif previous:
                                    pair["provider_exact_duplicates"] += 1
                            if pair["provider_conflicts"]:
                                pair["status"] = "PARTIAL_SOURCE_CONFLICT"
                    if pair["status"] not in {"PARTIAL_SOURCE_CONFLICT", "FAILED_VALIDATION"}:
                        ordered_times = sorted(page_times)
                        pair["last_page_oldest_two_utc"] = [value.isoformat() for value in ordered_times[:2]]
                        pair["next_position"] = position + max(1, raw_count - 2)
                        pair["status"] = "IN_PROGRESS"
                manifest["status"] = "AUDIT_RUNNING"
                if manifest["provider_calls"] >= MAX_TOTAL_CALLS and any(manifest["pairs"][k]["status"] in {"PENDING", "IN_PROGRESS"} for k in pair_ids):
                    manifest["status"] = "PARTIAL_PROVIDER_CALL_BUDGET"
                _atomic_json(run_root / "manifest.json", manifest, root=root)
                if progress:
                    progress({
                        "event": "PAGE_COMPLETE",
                        "request": manifest["provider_calls"],
                        "batch_request": calls_this_batch,
                        "symbol": pair["symbol"],
                        "timeframe": pair["timeframe"],
                        "position": position,
                        "returned": raw_count,
                        "oldest_utc": page_entry["oldest_utc"],
                        "status": pair["status"],
                    })
            if not progress_made:
                break
            if calls_this_batch >= max_provider_calls:
                break

        pending = any(pair["status"] in {"PENDING", "IN_PROGRESS"} for pair in manifest["pairs"].values())
        if pending:
            manifest["status"] = "PARTIAL_PROVIDER_CALL_BUDGET"
        elif any(pair["status"] == "PARTIAL_SOURCE_CONFLICT" for pair in manifest["pairs"].values()):
            manifest["status"] = "PARTIAL_SOURCE_CONFLICT"
        elif any(pair["status"] == "FAILED_VALIDATION" for pair in manifest["pairs"].values()):
            manifest["status"] = "FAILED_VALIDATION"
        elif any(pair["status"] not in {"COMPLETE_VS_MT5_OBSERVED"} for pair in manifest["pairs"].values()):
            manifest["status"] = "PARTIAL_MT5_HISTORY_LIMIT"
        else:
            manifest["status"] = "COMPLETE_VS_MT5_OBSERVED"
        _atomic_json(run_root / "manifest.json", manifest, root=root)
        return manifest
    except SafetyGateError as exc:
        manifest["status"] = "BLOCKED_SAFETY"
        manifest["error_code"] = str(exc)
        manifest["tradeapi_disabled_verified"] = False
        _atomic_json(run_root / "manifest.json", manifest, root=root)
        raise
    finally:
        try:
            api.shutdown()
        except Exception:
            pass


def _load_pair_bars(root: Path, run_root: Path, pair: dict[str, Any], *, page_limit: int | None = None) -> dict[datetime, dict[str, Any]]:
    result: dict[datetime, dict[str, Any]] = {}
    pages = pair.get("pages", [])
    if page_limit is not None:
        pages = pages[:page_limit]
    for entry in pages:
        relative = entry.get("path")
        path = ensure_no_symlink_path(root.resolve(), root / relative)
        raw = path.read_bytes()
        if _sha256(raw) != entry.get("raw_sha256"):
            raise CandleSyncError("PRIVATE_CHUNK_HASH_MISMATCH")
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("symbol") != pair["symbol"] or payload.get("timeframe") != pair["timeframe"]:
            raise CandleSyncError("PRIVATE_CHUNK_PAIR_MISMATCH")
        for raw_bar in payload.get("rows", []):
            bar = _normalize_rate(raw_bar)
            existing = result.get(bar["time"])
            if existing and _signature(existing) != _signature(bar):
                pair.setdefault("provider_conflicts", []).append({"time_utc": bar["time"].isoformat(), "kind": "PROVIDER_DUPLICATE_CONFLICT"})
                continue
            result[bar["time"]] = bar
    return result


def _csv_bytes(fields: list[str], rows: list[dict[str, Any]], line_ending: str) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore", lineterminator=line_ending)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _provider_row(bar: dict[str, Any], fields: list[str]) -> dict[str, str]:
    row = {field: "" for field in fields}
    row.update({
        "time": bar["time"].astimezone(timezone.utc).isoformat(),
        "open": _float_text(bar["open"]),
        "high": _float_text(bar["high"]),
        "low": _float_text(bar["low"]),
        "close": _float_text(bar["close"]),
        "tick_volume": _float_text(bar["tick_volume"]),
        "spread": str(int(bar["spread"])),
        "real_volume": _float_text(bar["real_volume"]),
    })
    return row


def _gaps(rows: list[tuple[datetime, dict[str, Any]]], timeframe: str) -> int:
    seconds = TIMEFRAMES_SECONDS[timeframe]
    times = sorted(timestamp for timestamp, _ in rows)
    return sum(1 for left, right in zip(times, times[1:]) if (right - left).total_seconds() > seconds * 1.5)


def sync_from_run(root: Path, run_id: str, *, idempotency_check: bool = False) -> dict[str, Any]:
    """Reconcile cached immutable pages and atomically update nonconflicting CSVs."""
    run_root = root / "reports" / "private" / "phase2_2_2f" / run_id
    inventory = _inventory_and_backup(root, run_id)
    _verify_preserved_inputs(root, inventory)
    manifest = _load_manifest(run_root, run_id, inventory)
    if manifest.get("status") == "BLOCKED_SAFETY" or manifest.get("account_type") != "REAL" or manifest.get("tradeapi_disabled_verified") is not True:
        raise SafetyGateError("READ_ONLY_REAL_ACCOUNT_GATE_NOT_VERIFIED_FOR_SYNC")
    frozen_hashes = file_content_hashes(root / FROZEN_DATASET)
    if frozen_hashes["canonical_sha256"] != FROZEN_DATASET_CANONICAL_SHA256:
        raise CandleSyncError("FROZEN_DATASET_HASH_MISMATCH")
    results: list[dict[str, Any]] = []
    source_changed = False
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            pair = manifest["pairs"][_pair_id(symbol, timeframe)]
            local_path = root / "data" / "market" / symbol / f"{timeframe}.csv"
            local = _validate_local(local_path, timeframe)
            record = _base_timeframe_report(symbol, timeframe, local)
            record["provider_calls"] = int(pair.get("provider_calls", 0))
            record["provider_first_utc"] = pair.get("provider_first_utc")
            record["provider_oldest_boundary_verified"] = pair.get("provider_oldest_boundary_verified") is True
            record["open_or_future_rows_excluded"] = int(pair.get("open_or_future_rows_excluded", 0))
            record["source_conflicts"] = len(pair.get("provider_conflicts", []))
            expected_current_hash = manifest.get("csv_hashes_after_sync", {}).get(
                _pair_id(symbol, timeframe), inventory["source_csv_hashes"].get(local_path.relative_to(root).as_posix())
            )
            if local["raw_sha256"] != expected_current_hash:
                record["status"] = "FAILED_VALIDATION"
                record["error"] = "SOURCE_CSV_CHANGED_AFTER_PRE_SYNC_BACKUP"
                source_changed = True
                results.append(record)
                continue
            if pair.get("status") in {"BLOCKED_SYMBOL_NOT_VISIBLE", "FAILED_VALIDATION", "PARTIAL_SOURCE_CONFLICT"}:
                record["status"] = pair.get("status")
                record["error"] = pair.get("error_code")
                results.append(record)
                continue
            try:
                provider = _load_pair_bars(root, run_root, pair)
            except (CandleSyncError, OSError, ValueError, KeyError) as exc:
                record["status"] = "FAILED_VALIDATION"
                record["error"] = str(exc) if str(exc).startswith("PRIVATE_") else type(exc).__name__
                results.append(record)
                continue
            record["source_conflicts"] = len(pair.get("provider_conflicts", []))
            if record["source_conflicts"]:
                record["status"] = "PARTIAL_SOURCE_CONFLICT"
                results.append(record)
                continue
            local_by_time = {timestamp: row for timestamp, row in local["rows"]}
            conflict_details: list[dict[str, Any]] = []
            new_bars: list[tuple[datetime, dict[str, Any]]] = []
            for timestamp, bar in sorted(provider.items()):
                if timestamp + timedelta(seconds=TIMEFRAMES_SECONDS[timeframe] + (3600 if timeframe == "D1" else 0)) > _utc(manifest["as_of_utc"]):
                    continue
                old = local_by_time.get(timestamp)
                if old is None:
                    new_bars.append((timestamp, bar))
                elif _signature({key: old[key] for key in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")}) != _signature(bar):
                    conflict_details.append({"time_utc": timestamp.isoformat(), "kind": "LOCAL_PROVIDER_CONTENT_CONFLICT"})
            if conflict_details:
                private_conflicts = run_root / f"source_conflicts_{symbol}_{timeframe}.json"
                _atomic_json(private_conflicts, {"symbol": symbol, "timeframe": timeframe, "conflicts": conflict_details}, root=root)
                record["status"] = "PARTIAL_SOURCE_CONFLICT"
                record["source_conflicts"] = len(conflict_details)
                results.append(record)
                continue
            new_rows = [_provider_row(bar, local["fields"]) for _, bar in new_bars]
            all_rows = [(timestamp, dict(row)) for timestamp, row in local["rows"]]
            all_rows.extend(new_bars)
            all_rows.sort(key=lambda item: item[0])
            line_ending = "\r\n" if b"\r\n" in local["raw"] else "\n"
            candidate = _csv_bytes(local["fields"], [row for _, row in all_rows], line_ending)
            if _sha256(local_path.read_bytes()) != local["raw_sha256"]:
                record["status"] = "FAILED_VALIDATION"
                record["error"] = "SOURCE_CSV_CHANGED_DURING_SYNC"
                source_changed = True
                results.append(record)
                continue
            if candidate != local["raw"]:
                safe_atomic_write_bytes(local_path, candidate, authorized_root=root, overwrite=True)
            after = _validate_local(local_path, timeframe)
            record.update({
                "local_first_after_utc": after["first_utc"],
                "local_last_after_utc": after["last_utc"],
                "rows_after": after["row_count"],
                "new_rows": len(new_rows),
                "new_rows_older_than_local": sum(1 for timestamp, _ in new_bars if local["rows"] and timestamp < local["rows"][0][0]),
                "new_rows_newer_than_local": sum(1 for timestamp, _ in new_bars if local["rows"] and timestamp > local["rows"][-1][0]),
                "new_rows_inside_local_range": sum(1 for timestamp, _ in new_bars if local["rows"] and local["rows"][0][0] < timestamp < local["rows"][-1][0]),
                "exact_duplicates_collapsed": int(pair.get("provider_exact_duplicates", 0)) + sum(1 for timestamp in provider if timestamp in local_by_time),
                "spread_zero_rows": sum(1 for bar in provider.values() if int(bar["spread"]) == 0),
                "unconfirmed_gaps_after": _gaps(after["rows"], timeframe),
                "raw_sha256": after["raw_sha256"],
                "canonical_sha256": after["canonical_sha256"],
            })
            if pair.get("status") == "COMPLETE_VS_MT5_OBSERVED" and pair.get("provider_oldest_boundary_verified") is True:
                record["status"] = "COMPLETE_VS_MT5_OBSERVED"
            elif manifest.get("status") == "PARTIAL_PROVIDER_CALL_BUDGET" or pair.get("status") == "IN_PROGRESS":
                record["status"] = "PARTIAL_PROVIDER_CALL_BUDGET"
            elif pair.get("status") == "PARTIAL_PROVIDER_LIMIT_UNKNOWN":
                record["status"] = "PARTIAL_MT5_HISTORY_LIMIT"
            else:
                record["status"] = pair.get("status", "PENDING")
            results.append(record)

    if source_changed:
        status = "FAILED_VALIDATION"
    elif any(item["status"] == "PARTIAL_SOURCE_CONFLICT" for item in results):
        status = "PARTIAL_SOURCE_CONFLICT"
    elif any(item["status"] == "FAILED_VALIDATION" for item in results):
        status = "FAILED_VALIDATION"
    elif any(item["status"] == "PARTIAL_PROVIDER_CALL_BUDGET" for item in results):
        status = "PARTIAL_PROVIDER_CALL_BUDGET"
    elif all(item["status"] == "COMPLETE_VS_MT5_OBSERVED" for item in results):
        status = "COMPLETE_VS_MT5_OBSERVED"
    else:
        status = "PARTIAL_MT5_HISTORY_LIMIT"

    previous_sync_hashes = manifest.get("csv_hashes_after_sync", {})
    current_hashes = {
        item["symbol"] + "|" + item["timeframe"]: file_content_hashes(root / "data" / "market" / item["symbol"] / f"{item['timeframe']}.csv")["raw_sha256"]
        for item in results
    }
    if idempotency_check and previous_sync_hashes:
        idempotent = previous_sync_hashes == current_hashes and all(item["new_rows"] == 0 for item in results)
    else:
        idempotent = None
    manifest["csv_hashes_after_sync"] = current_hashes
    manifest["sync_status"] = status
    manifest["sync_completed_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["idempotency_second_pass"] = idempotent
    _atomic_json(run_root / "manifest.json", manifest, root=root)
    return {
        "status": status,
        "idempotency_second_pass": idempotent,
        "provider_calls": int(manifest.get("provider_calls", 0)),
        "as_of_utc": manifest["as_of_utc"],
        "items": results,
        "orders_sent": 0,
        "trades_executed": 0,
        "mt5_reads_in_sync_stage": 0,
        "source_csv_hashes": current_hashes,
        "source_conflicts_private": sum(item["source_conflicts"] for item in results),
    }


def public_summary(root: Path, run_id: str, sync_result: dict[str, Any] | None = None) -> dict[str, Any]:
    run_root = root / "reports" / "private" / "phase2_2_2f" / run_id
    inventory = json.loads((run_root / "pre_sync_inventory.json").read_text(encoding="utf-8"))
    _verify_preserved_inputs(root, inventory)
    manifest = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    summary_items: list[dict[str, Any]] = []
    sync_map = {(item["symbol"], item["timeframe"]): item for item in (sync_result or {}).get("items", [])}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            key = _pair_id(symbol, timeframe)
            pair = manifest["pairs"][key]
            before = next(item for item in inventory["files"] if item["symbol"] == symbol and item["timeframe"] == timeframe)
            sync = sync_map.get((symbol, timeframe), {})
            summary_items.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "first_candle_before_utc": before["first_utc"],
                "first_candle_after_utc": sync.get("local_first_after_utc", before["first_utc"]),
                "last_candle_before_utc": before["last_utc"],
                "provider_first_utc": pair.get("provider_first_utc"),
                "last_closed_provider_utc": pair.get("provider_latest_utc"),
                "rows_before": before["row_count"],
                "rows_after": sync.get("rows_after", before["row_count"]),
                # Report total growth from the immutable pre-sync inventory.
                # On the idempotency pass, new_rows is correctly zero for that
                # pass, but must not erase the additions made by the first pass.
                "new_bars": max(0, sync.get("rows_after", before["row_count"]) - before["row_count"]),
                "unconfirmed_gaps": sync.get("unconfirmed_gaps_after"),
                "provider_queries": int(pair.get("provider_calls", 0)),
                "provider_boundary_verified": pair.get("provider_oldest_boundary_verified") is True,
                "status": sync.get("status", pair.get("status", "PENDING")),
                "raw_sha256_after": sync.get("raw_sha256"),
                "canonical_sha256_after": sync.get("canonical_sha256"),
            })
    added = sum(item["new_bars"] for item in summary_items)
    queries = int(manifest.get("provider_calls", 0))
    if sync_result:
        status = sync_result["status"]
    else:
        status = manifest.get("status", "PENDING")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "run_id": run_id,
        "as_of_utc": manifest["as_of_utc"],
        "system_local_date": datetime.now().astimezone().date().isoformat(),
        "account_type": "REAL" if manifest.get("account_type") == "REAL" else "UNKNOWN",
        "read_only_safety": "PASS" if manifest.get("tradeapi_disabled_verified") is True else ("FAIL" if manifest.get("status") == "BLOCKED_SAFETY" else "PENDING"),
        "terminal_maxbars_observed": manifest.get("terminal_maxbars_observed"),
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
        "provider_bar_queries": queries,
        "provider_query_cap": MAX_TOTAL_CALLS,
        "total_bars_added": added,
        "idempotency_second_pass_new_bars": (
            sum(item.get("new_rows", 0) for item in (sync_result or {}).get("items", []))
            if (sync_result or {}).get("idempotency_second_pass") is True else None
        ),
        "frozen_dataset_unchanged": inventory.get("frozen_dataset", {}).get("unchanged_expected") is True,
        "frozen_dataset_canonical_sha256": inventory.get("frozen_dataset", {}).get("canonical_sha256"),
        "preserved_snapshots_and_prior_chunks_unchanged": True,
        "idempotency_second_pass": (sync_result or {}).get("idempotency_second_pass"),
        "private_report_path": f"reports/private/phase2_2_2f/{run_id}",
        "items": summary_items,
    }


def write_public_reports(root: Path, summary: dict[str, Any]) -> None:
    report_dir = root / "reports" / "phase2_2_2f"
    report_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(report_dir / "validation_summary.json", summary, root=root)
    rows = [
        "| Símbolo | TF | Primera vela | Última vela cerrada | Filas | Nuevas | Huecos no confirmados | Estado |",
        "|---|---|---|---|---:|---:|---:|---|",
    ]
    for item in summary["items"]:
        rows.append(
            f"| {item['symbol']} | {item['timeframe']} | {item['first_candle_after_utc'] or '—'} | {item['last_closed_provider_utc'] or '—'} | {item['rows_after']} | {item['new_bars']} | {item['unconfirmed_gaps'] if item['unconfirmed_gaps'] is not None else '—'} | {item['status']} |"
        )
    doc = [
        "# Fase 2.2.2f — Sincronización read-only de velas MT5",
        "",
        f"**Estado:** {summary['status']}",
        "",
        f"Corte UTC: `{summary['as_of_utc']}`. Fecha local del PC: `{summary['system_local_date']}`.",
        "",
        f"Cuenta REAL verificada y `tradeapi_disabled=True`: **{summary['read_only_safety']}**. El proceso envió 0 órdenes y ejecutó 0 operaciones.",
        "",
        f"Consultas de barras MT5: **{summary['provider_bar_queries']} / {summary['provider_query_cap']}**. Velas nuevas: **{summary['total_bars_added']}**.",
        "",
        f"Dataset congelado intacto: **{summary['frozen_dataset_unchanged']}**; SHA-256 canónico `{summary['frozen_dataset_canonical_sha256']}`.",
        "",
        "La tabla enumera exactamente los 15 pares símbolo/temporalidad. Los huecos son no confirmados: no se infiere cierre de mercado ni se fabrican velas.",
        "",
        *rows,
        "",
        f"Idempotencia de segundo pase: `{summary['idempotency_second_pass']}` (velas nuevas en ese pase: `{summary['idempotency_second_pass_new_bars']}`). Evidencia privada (incluye chunks brutos y copias previas): `{summary['private_report_path']}`.",
        "",
        "No se ejecutaron señales, análisis, backtests ni llamadas LLM. Los datos de cuenta y el contenido bruto no se publican.",
        "",
    ]
    doc_path = root / "docs" / "FASE2_2_2F_SOLO_VELAS_RESULTADOS.md"
    safe_atomic_write_bytes(doc_path, "\n".join(doc).encode("utf-8"), authorized_root=root, overwrite=True)

