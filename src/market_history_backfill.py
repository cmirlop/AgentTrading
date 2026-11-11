"""Bounded, resumable and fail-closed preparation for future MT5 history reads."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

from scripts.validate_mt5_real_readonly import (
    READ_ONLY_METHODS,
    SafetyGateError,
    _attr,
    _read,
    _verify_terminal_gate,
)
from src.market_history_audit import (
    REQUIRED_COLUMNS,
    SYMBOLS,
    TIMEFRAMES_SECONDS,
    parse_utc_timestamp,
    sha256_bytes,
)
from src.history_data_integrity import (
    CANONICAL_HASH_POLICY,
    backfill_run_paths,
    content_hashes,
    ensure_no_symlink_path,
    file_content_hashes,
    safe_atomic_write_bytes,
    validate_run_id,
)


MAX_BARS_PER_REQUEST = 1000
OVERLAP_BARS_EACH_SIDE = 2
FORBIDDEN_MT5_METHODS = frozenset({"order_send", "order_check", "symbol_select", "login"})
CSV_FIELDS = REQUIRED_COLUMNS


def _iso_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("TIMEZONE_REQUIRED")
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value), timezone.utc)
    return parse_utc_timestamp(str(value))


def plan_chunks(
    intervals: Iterable[dict[str, Any]],
    *,
    max_bars_per_request: int = MAX_BARS_PER_REQUEST,
    overlap_bars_each_side: int = OVERLAP_BARS_EACH_SIDE,
    symbols: Iterable[str] = SYMBOLS,
    timeframes: dict[str, int] = TIMEFRAMES_SECONDS,
) -> list[dict[str, Any]]:
    """Split missing bar-open ranges into bounded requests with context overlap."""
    if max_bars_per_request < 5 or overlap_bars_each_side < 1:
        raise ValueError("CHUNK_LIMIT_OR_OVERLAP_INVALID")
    core_capacity = max_bars_per_request - 2 * overlap_bars_each_side
    allowed_symbols = set(symbols)
    allowed_timeframes = set(timeframes)
    chunks: list[dict[str, Any]] = []
    for interval in intervals:
        symbol = str(interval.get("symbol", ""))
        timeframe = str(interval.get("timeframe", ""))
        if symbol not in allowed_symbols or timeframe not in allowed_timeframes:
            raise ValueError("INTERVAL_SYMBOL_OR_TIMEFRAME_INVALID")
        seconds = timeframes[timeframe]
        core_start = _iso_time(interval["missing_start_utc"])
        core_end = _iso_time(interval["missing_end_utc"])
        delta = int((core_end - core_start).total_seconds())
        if delta < 0 or delta % seconds:
            raise ValueError("INTERVAL_NOT_ALIGNED_TO_TIMEFRAME")
        total_bars = delta // seconds + 1
        if total_bars <= 0:
            continue
        offset = 0
        chunk_number = 0
        interval_id = str(interval.get("interval_id") or hashlib.sha256(
            f"{symbol}|{timeframe}|{core_start.isoformat()}|{core_end.isoformat()}".encode()
        ).hexdigest()[:16])
        while offset < total_bars:
            take = min(core_capacity, total_bars - offset)
            part_start = core_start + timedelta(seconds=offset * seconds)
            part_end = part_start + timedelta(seconds=(take - 1) * seconds)
            request_start = part_start - timedelta(seconds=overlap_bars_each_side * seconds)
            request_end = part_end + timedelta(seconds=overlap_bars_each_side * seconds)
            identity = f"{symbol}|{timeframe}|{interval_id}|{part_start.isoformat()}|{part_end.isoformat()}"
            chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            chunks.append({
                "chunk_id": chunk_id,
                "interval_id": interval_id,
                "chunk_number": chunk_number,
                "symbol": symbol,
                "timeframe": timeframe,
                "timeframe_seconds": seconds,
                "core_start_utc": part_start.isoformat(),
                "core_end_utc": part_end.isoformat(),
                "request_start_utc": request_start.isoformat(),
                "request_end_utc": request_end.isoformat(),
                "core_bar_count": take,
                "request_bar_cap": max_bars_per_request,
                "overlap_bars_each_side": overlap_bars_each_side,
            })
            offset += take
            chunk_number += 1
    return chunks


def verify_real_readonly_account(api: Any) -> dict[str, Any]:
    """Require a connected REAL account and verified terminal trade API block."""
    gate = _verify_terminal_gate(api)
    account = _read(api, "account_info")
    real_mode = getattr(api, "ACCOUNT_TRADE_MODE_REAL", None)
    if account is None or real_mode is None or _attr(account, "trade_mode") != real_mode:
        raise SafetyGateError("ACCOUNT_NOT_VERIFIED_REAL")
    return {"account_type": "REAL", "read_only_safety": gate, "account_currency_available": bool(_attr(account, "currency"))}


def safe_copy_rates_range(api: Any, chunk: dict[str, Any], timeframe_value: Any) -> Any:
    """Use the shared MT5 read allowlist; gate state is checked before/after."""
    if "copy_rates_range" not in READ_ONLY_METHODS:
        raise SafetyGateError("COPY_RATES_RANGE_NOT_ALLOWLISTED")
    if any(method in READ_ONLY_METHODS for method in FORBIDDEN_MT5_METHODS):
        raise SafetyGateError("FORBIDDEN_METHOD_ENTERED_READ_ALLOWLIST")
    start = _iso_time(chunk["request_start_utc"])
    end = _iso_time(chunk["request_end_utc"])
    if end <= start:
        raise ValueError("REQUEST_RANGE_INVALID")
    return _read(api, "copy_rates_range", chunk["symbol"], timeframe_value, start, end)


def _record_to_mapping(record: Any) -> dict[str, Any]:
    if hasattr(record, "_asdict"):
        return dict(record._asdict())
    if isinstance(record, dict):
        return dict(record)
    names = getattr(getattr(record, "dtype", None), "names", None)
    if names:
        return {name: record[name].item() if hasattr(record[name], "item") else record[name] for name in names}
    fields = getattr(record, "_fields", ())
    if fields:
        return {name: getattr(record, name) for name in fields}
    raise ValueError("MT5_RATE_RECORD_UNSUPPORTED")


def validate_mt5_rates(
    rates: Any,
    timeframe: str,
    *,
    as_of_utc: datetime,
) -> dict[str, Any]:
    """Normalize closed bars; exact duplicates collapse and conflicts remain visible."""
    if timeframe not in TIMEFRAMES_SECONDS:
        raise ValueError("TIMEFRAME_UNSUPPORTED")
    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
        raise ValueError("AS_OF_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    seconds = TIMEFRAMES_SECONDS[timeframe]
    now = as_of_utc.astimezone(timezone.utc)
    valid: list[dict[str, Any]] = []
    invalid_count = 0
    open_count = 0
    exact_duplicates = 0
    duplicate_conflicts: list[dict[str, Any]] = []
    by_timestamp: dict[datetime, dict[tuple[Any, ...], dict[str, Any]]] = {}
    rate_records = [] if rates is None else rates
    for raw_record in rate_records:
        try:
            raw = _record_to_mapping(raw_record)
            timestamp = _iso_time(raw.get("time"))
            normalized: dict[str, Any] = {"time": timestamp}
            for key in OHLC_COLUMNS_LOCAL:
                number = float(raw[key])
                if not math.isfinite(number) or number <= 0:
                    raise ValueError("OHLC_INVALID")
                normalized[key] = number
            if normalized["low"] > min(normalized["open"], normalized["close"]) or normalized["high"] < max(normalized["open"], normalized["close"]) or normalized["low"] > normalized["high"]:
                raise ValueError("OHLC_RELATION_INVALID")
            spread = int(raw["spread"])
            tick_volume = float(raw["tick_volume"])
            real_volume = float(raw["real_volume"])
            if spread < 0 or not math.isfinite(tick_volume) or not math.isfinite(real_volume) or tick_volume < 0 or real_volume < 0:
                raise ValueError("SPREAD_OR_VOLUME_INVALID")
            normalized.update({"tick_volume": tick_volume, "spread": spread, "real_volume": real_volume})
            if timestamp.timestamp() % seconds:
                raise ValueError("TIMESTAMP_GRID_MISALIGNED")
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            invalid_count += 1
            continue
        if timestamp + timedelta(seconds=seconds) > now:
            open_count += 1
            continue
        signature = tuple(normalized[key] for key in CSV_FIELDS)
        versions = by_timestamp.setdefault(timestamp, {})
        if signature in versions:
            exact_duplicates += 1
        else:
            if versions:
                duplicate_conflicts.append({"time_utc": timestamp.isoformat(), "variants": [list(k) for k in versions] + [list(signature)]})
            versions[signature] = normalized
    for timestamp in sorted(by_timestamp):
        valid.extend(by_timestamp[timestamp].values())
    return {
        "bars": valid,
        "invalid_bar_count": invalid_count,
        "open_or_future_bar_count": open_count,
        "identical_duplicate_count": exact_duplicates,
        "conflicting_duplicate_timestamp_count": len(duplicate_conflicts),
        "duplicate_conflicts": duplicate_conflicts,
        "bar_count": len(valid),
        "source_values_modified": False,
    }


OHLC_COLUMNS_LOCAL = ("open", "high", "low", "close")


def _atomic_write_bytes(path: Path, payload: bytes, *, authorized_root: Path, overwrite: bool = True) -> None:
    safe_atomic_write_bytes(path, payload, authorized_root=authorized_root, overwrite=overwrite)


def _atomic_write_json(path: Path, payload: dict[str, Any], *, authorized_root: Path) -> None:
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, raw, authorized_root=authorized_root)


def _serialize_csv(bars: list[dict[str, Any]]) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for bar in bars:
        writer.writerow({key: bar[key].isoformat() if key == "time" else bar[key] for key in CSV_FIELDS})
    return buffer.getvalue().encode("utf-8")


def _plan_snapshot(plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("schema_version") != 2 or plan.get("canonicalization_policy") != CANONICAL_HASH_POLICY:
        raise RuntimeError("BACKFILL_PLAN_SCHEMA_UNSUPPORTED")
    required = {"symbols", "timeframes_seconds", "source_file_hashes", "backfill_intervals", "chunk_policy"}
    if not required.issubset(plan):
        raise RuntimeError("BACKFILL_PLAN_SCHEMA_UNSUPPORTED")
    return {key: plan[key] for key in sorted(required)}


def _canonical_hash_view(hashes: dict[str, dict[str, str]]) -> dict[str, str]:
    return {path: value["canonical_sha256"] for path, value in sorted(hashes.items())}


def _hashes_match_portably(expected: dict[str, dict[str, str]], actual: dict[str, dict[str, str]]) -> bool:
    return (
        expected.keys() == actual.keys()
        and all(
            expected[path].get("canonicalization_policy") == actual[path].get("canonicalization_policy") == CANONICAL_HASH_POLICY
            and expected[path].get("canonical_sha256") == actual[path].get("canonical_sha256")
            for path in expected
        )
    )


def _plan_fingerprint(plan: dict[str, Any]) -> str:
    snapshot = _plan_snapshot(plan)
    chunks = plan_chunks(
        snapshot["backfill_intervals"],
        symbols=snapshot["symbols"],
        timeframes=snapshot["timeframes_seconds"],
        max_bars_per_request=snapshot["chunk_policy"]["max_bars_per_request"],
        overlap_bars_each_side=snapshot["chunk_policy"]["overlap_bars_each_side"],
    )
    payload = {
        "schema_version": 2,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "chunks": chunks,
        "source_canonical_sha256": _canonical_hash_view(snapshot["source_file_hashes"]),
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _load_or_create_manifest(
    path: Path,
    *,
    run_id: str,
    plan: dict[str, Any],
    original_hashes: dict[str, dict[str, str]],
    planned_chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    validate_run_id(run_id)
    snapshot = _plan_snapshot(plan)
    fingerprint = _plan_fingerprint(plan)
    if path.is_file():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("BACKFILL_MANIFEST_CORRUPT") from None
        if manifest.get("schema_version") != 2:
            raise RuntimeError("BACKFILL_MANIFEST_SCHEMA_UNSUPPORTED")
        if manifest.get("run_id") != run_id or manifest.get("plan_sha256") != fingerprint or manifest.get("plan_snapshot") != snapshot:
            raise RuntimeError("BACKFILL_RESUME_PROVENANCE_MISMATCH")
        if not _hashes_match_portably(snapshot["source_file_hashes"], original_hashes):
            raise RuntimeError("BACKFILL_RESUME_SOURCE_CHANGED")
        expected_plan = {chunk["chunk_id"]: chunk for chunk in planned_chunks}
        if manifest.get("planned_chunks") != expected_plan:
            raise RuntimeError("BACKFILL_MANIFEST_PLAN_CHUNKS_MISMATCH")
        return manifest
    return {
        "schema_version": 2,
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "IN_PROGRESS",
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "plan_sha256": fingerprint,
        "plan_snapshot": snapshot,
        "planned_chunks": {chunk["chunk_id"]: chunk for chunk in planned_chunks},
        "source_file_hashes_at_run": original_hashes,
        "chunks": {},
        "conflicts": [],
        "source_files_unchanged": None,
        "tradeapi_disabled_verified": True,
        "account_type": "REAL",
        "orders_sent": 0,
        "provider_calls": 0,
    }


def _source_hashes(
    root: Path,
    *,
    symbols: Iterable[str] = SYMBOLS,
    timeframes: dict[str, int] = TIMEFRAMES_SECONDS,
) -> dict[str, dict[str, str]]:
    sources = []
    expected_old = {
        root / "data/market" / symbol / f"{timeframe}.csv"
        for symbol in symbols for timeframe in timeframes
    }
    actual_old = {
        path
        for symbol in symbols
        for path in (root / "data/market" / symbol).glob("*.csv")
    }
    snapshot_root = root / "data/market_history/mt5_real_readonly/20261109T202112.171333Z"
    expected_recent = {snapshot_root / f"{symbol}_{timeframe}.csv" for symbol in symbols for timeframe in timeframes}
    actual_recent = {
        path
        for symbol in symbols
        for path in snapshot_root.glob(f"{symbol}_*.csv")
    }
    if actual_old - expected_old or actual_recent - expected_recent:
        raise RuntimeError("BACKFILL_SOURCE_INVENTORY_MISMATCH")
    for symbol in symbols:
        for timeframe in timeframes:
            sources.append(root / "data/market" / symbol / f"{timeframe}.csv")
            sources.append(snapshot_root / f"{symbol}_{timeframe}.csv")
    result: dict[str, dict[str, str]] = {}
    for path in sources:
        ensure_no_symlink_path(root.resolve(), path)
        if not path.is_file():
            raise RuntimeError(f"BACKFILL_SOURCE_MISSING:{path.as_posix()}")
        result[path.relative_to(root).as_posix()] = file_content_hashes(path)
    return result


def _source_bar_index(root: Path, symbol: str, timeframe: str) -> dict[datetime, list[dict[str, Any]]]:
    paths = [root / "data/market" / symbol / f"{timeframe}.csv", root / "data/market_history/mt5_real_readonly/20261109T202112.171333Z" / f"{symbol}_{timeframe}.csv"]
    index: dict[datetime, list[dict[str, Any]]] = {}
    for path in paths:
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                try:
                    timestamp = parse_utc_timestamp(row["time"])
                except (KeyError, ValueError, TypeError):
                    continue
                index.setdefault(timestamp, []).append({"source": path.relative_to(root).as_posix(), **{key: row.get(key) for key in CSV_FIELDS[1:]}})
    return index


def _bar_value_signature(bar: dict[str, Any]) -> tuple[str, ...]:
    from src.market_history_audit import _material_signature

    return _material_signature(bar)


def _detect_source_conflicts(chunk: dict[str, Any], bars: list[dict[str, Any]], source_index: dict[datetime, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    conflicts = []
    for bar in bars:
        sources = source_index.get(bar["time"], [])
        differing = [source for source in sources if _bar_value_signature(source) != _bar_value_signature(bar)]
        if differing:
            conflicts.append({
                "symbol": chunk["symbol"],
                "timeframe": chunk["timeframe"],
                "time_utc": bar["time"].isoformat(),
                "downloaded_variant": {key: bar[key] for key in CSV_FIELDS[1:]},
                "existing_variants": differing,
                "action": "PRESERVED_BOTH_REQUIRES_REVIEW",
            })
    return conflicts


def validate_backfill_run(
    root: Path,
    run_id: str,
    *,
    as_of_utc: datetime,
) -> dict[str, Any]:
    """Validate one explicit run, its frozen plan, chunk hashes and row provenance."""
    result: dict[str, Any] = {
        "run_id": run_id,
        "integrity_status": "BACKFILL_INTEGRITY_FAILED",
        "errors": [],
        "chunks": [],
        "pending_chunk_ids": [],
        "empty_chunk_ids": [],
        "source_conflicts": [],
        "records_by_symbol_timeframe": {},
        "row_count": 0,
        "accepted_chunk_count": 0,
        "plan_sha256": None,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
    }
    try:
        paths = backfill_run_paths(root, run_id)
        manifest_path = paths["manifest"]
        if not manifest_path.is_file():
            result["errors"].append("BACKFILL_MANIFEST_MISSING")
            return result
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 2 or manifest.get("canonicalization_policy") != CANONICAL_HASH_POLICY:
            result["errors"].append("BACKFILL_MANIFEST_SCHEMA_UNSUPPORTED")
            return result
        if manifest.get("run_id") != run_id:
            result["errors"].append("BACKFILL_RUN_ID_MISMATCH")
            return result
        snapshot = manifest.get("plan_snapshot")
        if not isinstance(snapshot, dict):
            result["errors"].append("BACKFILL_PLAN_SNAPSHOT_MISSING")
            return result
        plan = {"schema_version": 2, "canonicalization_policy": CANONICAL_HASH_POLICY, **snapshot}
        expected_fingerprint = _plan_fingerprint(plan)
        result["plan_sha256"] = expected_fingerprint
        if manifest.get("plan_sha256") != expected_fingerprint:
            result["errors"].append("BACKFILL_PLAN_FINGERPRINT_MISMATCH")
            return result

        symbols = snapshot["symbols"]
        timeframes = snapshot["timeframes_seconds"]
        source_hashes_now = _source_hashes(root, symbols=symbols, timeframes=timeframes)
        if not _hashes_match_portably(snapshot["source_file_hashes"], source_hashes_now):
            result["errors"].append("BACKFILL_ORIGINAL_SOURCE_HASH_MISMATCH")
            return result
        started_hashes = manifest.get("source_file_hashes_at_run")
        if not isinstance(started_hashes, dict) or not _hashes_match_portably(snapshot["source_file_hashes"], started_hashes):
            result["errors"].append("BACKFILL_SOURCE_PROVENANCE_MISMATCH")
            return result
        ended_hashes = manifest.get("source_file_hashes_after")
        if ended_hashes is not None and not _hashes_match_portably(snapshot["source_file_hashes"], ended_hashes):
            result["errors"].append("BACKFILL_ORIGINAL_SOURCE_CHANGED")
            return result
        if manifest.get("source_files_unchanged") is False or manifest.get("status") == "FAIL_SOURCE_MODIFIED":
            result["errors"].append("BACKFILL_ORIGINAL_SOURCE_CHANGED")
            return result

        chunks = plan_chunks(
            snapshot["backfill_intervals"],
            symbols=symbols,
            timeframes=timeframes,
            max_bars_per_request=snapshot["chunk_policy"]["max_bars_per_request"],
            overlap_bars_each_side=snapshot["chunk_policy"]["overlap_bars_each_side"],
        )
        expected_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
        if manifest.get("planned_chunks") != expected_by_id:
            result["errors"].append("BACKFILL_MANIFEST_PLAN_CHUNKS_MISMATCH")
            return result
        entries = manifest.get("chunks")
        if not isinstance(entries, dict) or not set(entries).issubset(expected_by_id):
            result["errors"].append("BACKFILL_MANIFEST_ORPHAN_CHUNK")
            return result

        chunks_dir = paths["chunks"]
        if chunks_dir.exists() and not chunks_dir.is_dir():
            result["errors"].append("BACKFILL_CHUNK_PATH_NOT_DIRECTORY")
            return result
        disk_entries = list(chunks_dir.iterdir()) if chunks_dir.exists() else []
        if any(path.is_symlink() for path in disk_entries):
            result["errors"].append("BACKFILL_SYMLINK_PATH_REJECTED")
            return result
        expected_filenames = {f"{chunk_id}.csv" for chunk_id in expected_by_id}
        extra_filenames = {path.name for path in disk_entries} - expected_filenames
        if extra_filenames:
            result["errors"].append("BACKFILL_ORPHAN_CHUNK_FILE")
            result["orphan_files"] = sorted(extra_filenames)
            return result

        originals: dict[tuple[str, str], dict[datetime, list[dict[str, Any]]]] = {}
        included: dict[tuple[str, str], dict[datetime, list[dict[str, Any]]]] = {}
        complete_states = {"COMPLETED", "COMPLETED_WITH_CONFLICTS", "RECOVERED_ATOMIC_CHUNK_FILE"}
        pending_states = {"FAILED", "REQUESTING"}
        integrity_error = False
        conflict_count_before = len(result["source_conflicts"])
        for chunk in chunks:
            chunk_id = chunk["chunk_id"]
            entry = entries.get(chunk_id)
            destination = ensure_no_symlink_path(root.resolve(), chunks_dir / f"{chunk_id}.csv")
            if entry is None:
                if destination.exists():
                    result["errors"].append("BACKFILL_ORPHAN_CHUNK_FILE")
                    integrity_error = True
                result["pending_chunk_ids"].append(chunk_id)
                result["chunks"].append({"chunk_id": chunk_id, "status": "NOT_REQUESTED", "accepted": False})
                continue
            if entry.get("planned_chunk") != chunk:
                result["errors"].append("BACKFILL_MANIFEST_PLAN_CHUNKS_MISMATCH")
                integrity_error = True
                continue
            status = entry.get("status")
            expected_path = (paths["run_root"] / "chunks" / f"{chunk_id}.csv").relative_to(root.resolve()).as_posix()
            if status not in complete_states | pending_states:
                result["errors"].append("BACKFILL_CHUNK_STATUS_INVALID")
                integrity_error = True
                continue
            if entry.get("file") not in (None, expected_path):
                result["errors"].append("BACKFILL_CHUNK_PATH_MISMATCH")
                integrity_error = True
                continue
            if status in pending_states:
                if destination.exists() and status != "REQUESTING":
                    result["errors"].append("BACKFILL_ORPHAN_CHUNK_FILE")
                    integrity_error = True
                    continue
                result["pending_chunk_ids"].append(chunk_id)
                result["chunks"].append({"chunk_id": chunk_id, "status": status, "accepted": False})
                continue
            if not destination.is_file():
                result["errors"].append("BACKFILL_COMPLETED_CHUNK_MISSING")
                integrity_error = True
                continue
            raw_bytes = destination.read_bytes()
            actual_hashes = content_hashes(raw_bytes)
            if entry.get("canonicalization_policy") != CANONICAL_HASH_POLICY or actual_hashes["canonical_sha256"] != entry.get("canonical_sha256"):
                result["errors"].append("BACKFILL_CHUNK_HASH_MISMATCH")
                integrity_error = True
                continue
            if entry.get("raw_sha256") != actual_hashes["raw_sha256"]:
                # CRLF/LF-only change is accepted, but explicitly surfaced.
                raw_hash_status = "CANONICAL_MATCH_RAW_BYTES_DIFFER"
            else:
                raw_hash_status = "RAW_AND_CANONICAL_MATCH"
            if entry.get("request_start_utc") != chunk["request_start_utc"] or entry.get("request_end_utc") != chunk["request_end_utc"]:
                result["errors"].append("BACKFILL_CHUNK_REQUEST_RANGE_MISMATCH")
                integrity_error = True
                continue

            from src.market_history_audit import _load_csv

            parsed = _load_csv(destination, chunk["timeframe"], as_of_utc)
            empty = bool(entry.get("empty_response")) and parsed["row_count"] == 0 and entry.get("bar_count") == 0
            missing_fields = parsed["missing_required_field_counts"]
            has_missing_fields = any(value for value in missing_fields.values())
            if not empty and (
                parsed["ohlc_integrity_status"] != "PASS"
                or parsed["row_count"] != entry.get("bar_count")
                or parsed["invalid_volume_count"]
                or has_missing_fields
            ):
                result["errors"].append("BACKFILL_CHUNK_DATA_INVALID")
                integrity_error = True
                continue
            start, end = _iso_time(chunk["request_start_utc"]), _iso_time(chunk["request_end_utc"])
            if any(not start <= record["time"] <= end for record in parsed["_records"]):
                result["errors"].append("BACKFILL_CHUNK_TIMESTAMP_OUT_OF_RANGE")
                integrity_error = True
                continue
            if empty:
                result["empty_chunk_ids"].append(chunk_id)
                result["pending_chunk_ids"].append(chunk_id)
                result["chunks"].append({
                    "chunk_id": chunk_id,
                    "status": status,
                    "accepted": True,
                    "empty": True,
                    "hash_status": raw_hash_status,
                    "raw_sha256_expected": entry.get("raw_sha256"),
                    "raw_sha256_actual": actual_hashes["raw_sha256"],
                    "canonical_sha256_expected": entry.get("canonical_sha256"),
                    "canonical_sha256_actual": actual_hashes["canonical_sha256"],
                    "canonicalization_policy": CANONICAL_HASH_POLICY,
                })
                continue

            key = (chunk["symbol"], chunk["timeframe"])
            if key not in originals:
                originals[key] = _source_bar_index(root, *key)
            target = included.setdefault(key, {})
            conflicts_before_chunk = len(result["source_conflicts"])
            for record in parsed["_records"]:
                row_values = {column: record["row"].get(column) for column in CSV_FIELDS[1:]}
                candidate = {"source": f"backfill:{run_id}:{chunk_id}", **row_values}
                existing_variants = originals[key].get(record["time"], []) + target.get(record["time"], [])
                differing = [prior for prior in existing_variants if _bar_value_signature(prior) != _bar_value_signature(candidate)]
                if differing:
                    conflict = {
                        "status": "BACKFILL_SOURCE_CONFLICT_REQUIRES_REVIEW",
                        "symbol": chunk["symbol"],
                        "timeframe": chunk["timeframe"],
                        "time_utc": record["time"].isoformat(),
                        "downloaded_variant": candidate,
                        "existing_variants": differing,
                        "chunk_id": chunk_id,
                    }
                    result["source_conflicts"].append(conflict)
                if not any(_bar_value_signature(prior) == _bar_value_signature(candidate) for prior in target.get(record["time"], [])):
                    target.setdefault(record["time"], []).append(candidate)

            observed_chunk_conflicts = len(result["source_conflicts"]) - conflicts_before_chunk
            if int(entry.get("source_conflict_count", 0)) > observed_chunk_conflicts:
                result["errors"].append("BACKFILL_CONFLICT_EVIDENCE_MISSING")
                integrity_error = True
                continue

            result["accepted_chunk_count"] += 1
            result["row_count"] += parsed["row_count"]
            result["chunks"].append({
                "chunk_id": chunk_id,
                "status": status,
                "accepted": True,
                "bar_count": parsed["row_count"],
                "hash_status": raw_hash_status,
                "raw_sha256_expected": entry.get("raw_sha256"),
                "raw_sha256_actual": actual_hashes["raw_sha256"],
                "canonical_sha256_expected": entry.get("canonical_sha256"),
                "canonical_sha256_actual": actual_hashes["canonical_sha256"],
                "canonicalization_policy": CANONICAL_HASH_POLICY,
            })

        if manifest.get("conflicts"):
            for conflict in manifest["conflicts"]:
                if conflict not in result["source_conflicts"]:
                    result["source_conflicts"].append({**conflict, "status": "BACKFILL_SOURCE_CONFLICT_REQUIRES_REVIEW"})
        result["records_by_symbol_timeframe"] = {
            f"{symbol}|{timeframe}": [
                {"time": timestamp, "row": variant, "signature": tuple(variant.get(key) for key in CSV_FIELDS[1:]), "source": variant["source"]}
                for timestamp, variants in sorted(index.items())
                for variant in variants
            ]
            for (symbol, timeframe), index in included.items()
        }
        if result["errors"] or integrity_error:
            result["integrity_status"] = "BACKFILL_INTEGRITY_FAILED"
        elif result["source_conflicts"]:
            result["integrity_status"] = "BACKFILL_SOURCE_CONFLICT_REQUIRES_REVIEW"
        else:
            result["integrity_status"] = "PASS"
        result["planned_chunk_count"] = len(chunks)
        result["manifest_status_untrusted"] = manifest.get("status")
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, json.JSONDecodeError) as exc:
        code = str(exc) if str(exc).startswith("BACKFILL_") else type(exc).__name__.upper()
        result["errors"].append(code)
        result["integrity_status"] = "BACKFILL_INTEGRITY_FAILED"
        return result


def execute_backfill_plan(
    api: Any,
    plan: dict[str, Any],
    *,
    root: Path,
    run_id: str,
    as_of_utc: datetime,
) -> dict[str, Any]:
    """Execute only an explicit plan; completed, hash-verified chunks resume safely."""
    validate_run_id(run_id)
    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
        raise ValueError("AS_OF_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    snapshot = _plan_snapshot(plan)
    symbols = snapshot["symbols"]
    timeframes = snapshot["timeframes_seconds"]
    source_hashes_before = _source_hashes(root, symbols=symbols, timeframes=timeframes)
    if not _hashes_match_portably(snapshot["source_file_hashes"], source_hashes_before):
        raise RuntimeError("BACKFILL_PLAN_SOURCE_CHANGED_RERUN_AUDIT")
    backfill_run_paths(root, run_id, create=False)
    verify_real_readonly_account(api)
    paths = backfill_run_paths(root, run_id, create=True)
    output_root, chunks_dir, manifest_path = paths["run_root"], paths["chunks"], paths["manifest"]
    chunks = plan_chunks(
        plan.get("backfill_intervals", []),
        symbols=symbols,
        timeframes=timeframes,
        max_bars_per_request=snapshot["chunk_policy"]["max_bars_per_request"],
        overlap_bars_each_side=snapshot["chunk_policy"]["overlap_bars_each_side"],
    )
    manifest = _load_or_create_manifest(
        manifest_path,
        run_id=run_id,
        plan=plan,
        original_hashes=source_hashes_before,
        planned_chunks=chunks,
    )
    expected_chunk_ids = {chunk["chunk_id"] for chunk in chunks}
    if not set(manifest.get("chunks", {})).issubset(expected_chunk_ids):
        raise RuntimeError("BACKFILL_MANIFEST_ORPHAN_CHUNK")
    entries = list(chunks_dir.iterdir())
    if any(path.is_symlink() for path in entries):
        raise RuntimeError("BACKFILL_SYMLINK_PATH_REJECTED")
    if any(not path.is_file() for path in entries):
        raise RuntimeError("BACKFILL_ORPHAN_CHUNK_FILE")
    present_files = {path.name for path in entries if path.is_file()}
    allowed_files = {f"{chunk_id}.csv" for chunk_id in expected_chunk_ids}
    if present_files - allowed_files:
        raise RuntimeError("BACKFILL_ORPHAN_CHUNK_FILE")
    _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
    total_bars = 0
    provider_calls = 0
    source_indices: dict[tuple[str, str], dict[datetime, list[dict[str, Any]]]] = {}
    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        existing = manifest["chunks"].get(chunk_id)
        filename = f"{chunk_id}.csv"
        destination = ensure_no_symlink_path(output_root, chunks_dir / filename)
        if existing and existing.get("planned_chunk") != chunk:
            raise RuntimeError("BACKFILL_MANIFEST_PLAN_CHUNKS_MISMATCH")
        if existing and existing.get("status") in {"COMPLETED", "COMPLETED_WITH_CONFLICTS", "RECOVERED_ATOMIC_CHUNK_FILE"}:
            if not destination.is_file():
                raise RuntimeError("BACKFILL_COMPLETED_CHUNK_HASH_MISMATCH")
            actual_hashes = content_hashes(destination.read_bytes())
            if actual_hashes["canonical_sha256"] != existing.get("canonical_sha256"):
                raise RuntimeError("BACKFILL_COMPLETED_CHUNK_HASH_MISMATCH")
            continue
        if destination.exists() and (not existing or existing.get("status") == "REQUESTING"):
            # Recover the atomic-file-before-manifest interruption point.
            parsed = _read_normalized_csv(destination)
            start, end = _iso_time(chunk["request_start_utc"]), _iso_time(chunk["request_end_utc"])
            validation = validate_mt5_rates(parsed, chunk["timeframe"], as_of_utc=as_of_utc)
            if not parsed or validation["invalid_bar_count"] or any(not start <= bar["time"] <= end for bar in validation["bars"]):
                raise RuntimeError("BACKFILL_ORPHAN_CHUNK_INVALID")
            raw_bytes = destination.read_bytes()
            source_key = (chunk["symbol"], chunk["timeframe"])
            if source_key not in source_indices:
                source_indices[source_key] = _source_bar_index(root, *source_key)
            conflicts = _detect_source_conflicts(chunk, validation["bars"], source_indices[source_key])
            recovered_entry = {
                "status": "COMPLETED_WITH_CONFLICTS" if conflicts else "RECOVERED_ATOMIC_CHUNK_FILE",
                "file": destination.relative_to(root).as_posix(),
                "raw_sha256": sha256_bytes(raw_bytes),
                "canonical_sha256": content_hashes(raw_bytes)["canonical_sha256"],
                "canonicalization_policy": CANONICAL_HASH_POLICY,
                "bar_count": len(validation["bars"]),
                "planned_chunk": chunk,
                "request_start_utc": chunk["request_start_utc"],
                "request_end_utc": chunk["request_end_utc"],
                "provider_call_count": 1,
                "open_or_future_bar_count_excluded": 0,
                "source_conflict_count": len(conflicts),
            }
            manifest["chunks"][chunk_id] = recovered_entry
            manifest["conflicts"].extend(conflicts)
            _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
            continue
        if destination.exists():
            raise RuntimeError("BACKFILL_OUTPUT_WOULD_OVERWRITE_EXISTING_VERSION")
        timeframe_value = getattr(api, f"TIMEFRAME_{chunk['timeframe']}", None)
        if timeframe_value is None:
            raise SafetyGateError("TIMEFRAME_CONSTANT_UNAVAILABLE")
        previous_attempts = int((existing or {}).get("attempts", 0))
        previous_calls = int((existing or {}).get("provider_call_count", 0))
        attempt_number = previous_attempts + 1
        call_count_for_chunk = previous_calls + 1
        manifest["chunks"][chunk_id] = {
            **(existing or {}),
            "status": "REQUESTING",
            "file": None,
            "planned_chunk": chunk,
            "raw_sha256": None,
            "canonical_sha256": None,
            "attempts": attempt_number,
            "provider_call_count": call_count_for_chunk,
        }
        _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
        try:
            provider_calls += 1
            raw_rates = safe_copy_rates_range(api, chunk, timeframe_value)
            if raw_rates is None:
                raise RuntimeError("MT5_HISTORY_READ_RETURNED_NONE")
            validation = validate_mt5_rates(raw_rates, chunk["timeframe"], as_of_utc=as_of_utc)
            if validation["invalid_bar_count"] or validation["conflicting_duplicate_timestamp_count"]:
                raise RuntimeError("MT5_HISTORY_RETURNED_INVALID_OR_CONFLICTING_BARS")
            bars = validation["bars"]
            request_start = _iso_time(chunk["request_start_utc"])
            request_end = _iso_time(chunk["request_end_utc"])
            if any(not request_start <= bar["time"] <= request_end for bar in bars):
                raise RuntimeError("MT5_HISTORY_RETURNED_BAR_OUTSIDE_REQUEST_RANGE")
            source_key = (chunk["symbol"], chunk["timeframe"])
            if source_key not in source_indices:
                source_indices[source_key] = _source_bar_index(root, *source_key)
            conflicts = _detect_source_conflicts(chunk, bars, source_indices[source_key])
            csv_bytes = _serialize_csv(bars)
            _atomic_write_bytes(destination, csv_bytes, authorized_root=output_root, overwrite=False)
            hashes = content_hashes(csv_bytes)
            status = "COMPLETED_WITH_CONFLICTS" if conflicts else "COMPLETED"
            entry = {
                "status": status,
                "file": destination.relative_to(root).as_posix(),
                "raw_sha256": hashes["raw_sha256"],
                "canonical_sha256": hashes["canonical_sha256"],
                "canonicalization_policy": CANONICAL_HASH_POLICY,
                "planned_chunk": chunk,
                "attempts": attempt_number,
                "bar_count": len(bars),
                "open_or_future_bar_count_excluded": validation["open_or_future_bar_count"],
                "identical_duplicate_count_collapsed": validation["identical_duplicate_count"],
                "source_conflict_count": len(conflicts),
                "provider_call_count": call_count_for_chunk,
                "empty_response": len(bars) == 0,
                "tradeapi_disabled_verified_before_and_after": True,
                "request_start_utc": chunk["request_start_utc"],
                "request_end_utc": chunk["request_end_utc"],
            }
            manifest["chunks"][chunk_id] = entry
            manifest["conflicts"].extend(conflicts)
            total_bars += len(bars)
            _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
        except Exception as exc:
            manifest["chunks"][chunk_id] = {
                **manifest["chunks"][chunk_id],
                "status": "FAILED",
                "error_code": _safe_error_code(exc),
                "provider_call_count": call_count_for_chunk,
                "attempts": attempt_number,
            }
            manifest["status"] = "INCOMPLETE"
            _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
            raise

    source_hashes_after = _source_hashes(root, symbols=symbols, timeframes=timeframes)
    unchanged = source_hashes_before == source_hashes_after
    if not unchanged:
        manifest["status"] = "FAIL_SOURCE_MODIFIED"
        manifest["source_files_unchanged"] = False
        _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
        raise RuntimeError("BACKFILL_ORIGINAL_SOURCE_CHANGED")
    statuses = [entry.get("status") for entry in manifest["chunks"].values()]
    complete_statuses = {"COMPLETED", "COMPLETED_WITH_CONFLICTS", "RECOVERED_ATOMIC_CHUNK_FILE"}
    all_chunks_fetched = len(statuses) == len(chunks) and all(status in complete_statuses for status in statuses)
    has_empty_chunk = any(entry.get("empty_response") or entry.get("bar_count", 0) == 0 for entry in manifest["chunks"].values())
    manifest["status"] = "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT" if all_chunks_fetched and not manifest["conflicts"] and not has_empty_chunk else "HISTORY_BACKFILL_PREPARED_WITH_REVIEW"
    manifest["source_files_unchanged"] = True
    manifest["source_file_hashes_after"] = source_hashes_after
    manifest["provider_calls"] = sum(int(entry.get("provider_call_count", 0)) for entry in manifest["chunks"].values())
    manifest["bars_written_this_run"] = total_bars
    manifest["orders_sent"] = 0
    _atomic_write_json(manifest_path, manifest, authorized_root=output_root)
    return manifest


def _read_normalized_csv(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                timestamp = parse_utc_timestamp(row["time"])
                values = {key: row[key] for key in CSV_FIELDS[1:]}
            except (KeyError, ValueError, TypeError):
                return []
            rows.append({"time": timestamp, **values})
    return rows


def _safe_error_code(exc: Exception) -> str:
    text = str(exc)
    allowed = {
        "MT5_HISTORY_READ_RETURNED_NONE",
        "MT5_HISTORY_RETURNED_INVALID_OR_CONFLICTING_BARS",
        "BACKFILL_COMPLETED_CHUNK_HASH_MISMATCH",
        "BACKFILL_ORPHAN_CHUNK_INVALID",
        "BACKFILL_ORIGINAL_SOURCE_CHANGED",
        "BACKFILL_RESUME_PROVENANCE_MISMATCH",
        "BACKFILL_MANIFEST_CORRUPT",
    }
    if text in allowed:
        return text
    return type(exc).__name__.upper()
