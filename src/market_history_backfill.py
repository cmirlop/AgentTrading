"""Bounded, resumable and fail-closed preparation for future MT5 history reads."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
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
) -> list[dict[str, Any]]:
    """Split missing bar-open ranges into bounded requests with context overlap."""
    if max_bars_per_request < 5 or overlap_bars_each_side < 1:
        raise ValueError("CHUNK_LIMIT_OR_OVERLAP_INVALID")
    core_capacity = max_bars_per_request - 2 * overlap_bars_each_side
    chunks: list[dict[str, Any]] = []
    for interval in intervals:
        symbol = str(interval.get("symbol", ""))
        timeframe = str(interval.get("timeframe", ""))
        if symbol not in SYMBOLS or timeframe not in TIMEFRAMES_SECONDS:
            raise ValueError("INTERVAL_SYMBOL_OR_TIMEFRAME_INVALID")
        seconds = TIMEFRAMES_SECONDS[timeframe]
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


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write_bytes(path, raw)


def _serialize_csv(bars: list[dict[str, Any]]) -> bytes:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for bar in bars:
        writer.writerow({key: bar[key].isoformat() if key == "time" else bar[key] for key in CSV_FIELDS})
    return buffer.getvalue().encode("utf-8")


def _load_or_create_manifest(path: Path, *, run_id: str, plan: dict[str, Any], original_hashes: dict[str, str]) -> dict[str, Any]:
    chunks = plan_chunks(plan.get("backfill_intervals", []))
    plan_fingerprint = sha256_bytes(json.dumps({"chunks": chunks, "source_sha256": plan.get("source_files_sha256")}, sort_keys=True, separators=(",", ":")).encode())
    if path.is_file():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("BACKFILL_MANIFEST_CORRUPT") from None
        if manifest.get("run_id") != run_id or manifest.get("plan_sha256") != plan_fingerprint or manifest.get("source_sha256") != original_hashes:
            raise RuntimeError("BACKFILL_RESUME_PROVENANCE_MISMATCH")
        return manifest
    return {
        "schema_version": 1,
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "IN_PROGRESS",
        "plan_sha256": plan_fingerprint,
        "source_sha256": original_hashes,
        "chunks": {},
        "conflicts": [],
        "source_files_unchanged": None,
        "tradeapi_disabled_verified": True,
        "account_type": "REAL",
        "orders_sent": 0,
        "provider_calls": 0,
    }


def _source_hashes(root: Path) -> dict[str, str]:
    sources = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES_SECONDS:
            sources.append(root / "data/market" / symbol / f"{timeframe}.csv")
            sources.append(root / "data/market_history/mt5_real_readonly/20261109T202112.171333Z" / f"{symbol}_{timeframe}.csv")
    result = {}
    for path in sources:
        if not path.is_file():
            raise RuntimeError(f"BACKFILL_SOURCE_MISSING:{path.as_posix()}")
        result[path.relative_to(root).as_posix()] = sha256_bytes(path.read_bytes())
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
    values: list[str] = []
    for key in CSV_FIELDS[1:]:
        value = bar.get(key)
        if key in OHLC_COLUMNS_LOCAL:
            try:
                value = format(float(value), ".12g")
            except (TypeError, ValueError, OverflowError):
                value = str(value)
        else:
            value = str(value)
        values.append(value)
    return tuple(values)


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


def execute_backfill_plan(
    api: Any,
    plan: dict[str, Any],
    *,
    root: Path,
    run_id: str,
    as_of_utc: datetime,
) -> dict[str, Any]:
    """Execute only an explicit plan; completed, hash-verified chunks resume safely."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", run_id):
        raise ValueError("RUN_ID_INVALID")
    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
        raise ValueError("AS_OF_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    source_hashes_before = _source_hashes(root)
    if plan.get("source_files_sha256") != source_hashes_before:
        raise RuntimeError("BACKFILL_PLAN_SOURCE_CHANGED_RERUN_AUDIT")
    verify_real_readonly_account(api)
    output_root = root / "data/market_history/backfill_runs" / run_id
    chunks_dir = output_root / "chunks"
    manifest_path = output_root / "manifest.json"
    chunks = plan_chunks(plan.get("backfill_intervals", []))
    manifest = _load_or_create_manifest(manifest_path, run_id=run_id, plan=plan, original_hashes=source_hashes_before)
    _atomic_write_json(manifest_path, manifest)
    total_bars = 0
    provider_calls = 0
    source_indices: dict[tuple[str, str], dict[datetime, list[dict[str, Any]]]] = {}
    for chunk in chunks:
        chunk_id = chunk["chunk_id"]
        existing = manifest["chunks"].get(chunk_id)
        filename = f"{chunk_id}.csv"
        destination = chunks_dir / filename
        if existing and existing.get("status") in {"COMPLETED", "COMPLETED_WITH_CONFLICTS"}:
            if not destination.is_file() or sha256_bytes(destination.read_bytes()) != existing.get("sha256"):
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
                "sha256": sha256_bytes(raw_bytes),
                "bar_count": len(validation["bars"]),
                "provider_call_count": 1,
                "open_or_future_bar_count_excluded": 0,
                "source_conflict_count": len(conflicts),
            }
            manifest["chunks"][chunk_id] = recovered_entry
            manifest["conflicts"].extend(conflicts)
            _atomic_write_json(manifest_path, manifest)
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
            "sha256": None,
            "attempts": attempt_number,
            "provider_call_count": call_count_for_chunk,
        }
        _atomic_write_json(manifest_path, manifest)
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
            _atomic_write_bytes(destination, csv_bytes)
            sha = sha256_bytes(csv_bytes)
            status = "COMPLETED_WITH_CONFLICTS" if conflicts else "COMPLETED"
            entry = {
                "status": status,
                "file": destination.relative_to(root).as_posix(),
                "sha256": sha,
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
            _atomic_write_json(manifest_path, manifest)
        except Exception as exc:
            manifest["chunks"][chunk_id] = {
                **manifest["chunks"][chunk_id],
                "status": "FAILED",
                "error_code": _safe_error_code(exc),
                "provider_call_count": call_count_for_chunk,
                "attempts": attempt_number,
            }
            manifest["status"] = "INCOMPLETE"
            _atomic_write_json(manifest_path, manifest)
            raise

    source_hashes_after = _source_hashes(root)
    unchanged = source_hashes_before == source_hashes_after
    if not unchanged:
        manifest["status"] = "FAIL_SOURCE_MODIFIED"
        manifest["source_files_unchanged"] = False
        _atomic_write_json(manifest_path, manifest)
        raise RuntimeError("BACKFILL_ORIGINAL_SOURCE_CHANGED")
    statuses = [entry.get("status") for entry in manifest["chunks"].values()]
    complete_statuses = {"COMPLETED", "COMPLETED_WITH_CONFLICTS", "RECOVERED_ATOMIC_CHUNK_FILE"}
    all_chunks_fetched = len(statuses) == len(chunks) and all(status in complete_statuses for status in statuses)
    has_empty_chunk = any(entry.get("empty_response") or entry.get("bar_count", 0) == 0 for entry in manifest["chunks"].values())
    manifest["status"] = "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT" if all_chunks_fetched and not manifest["conflicts"] and not has_empty_chunk else "HISTORY_BACKFILL_PREPARED_WITH_REVIEW"
    manifest["source_files_unchanged"] = True
    manifest["source_sha256_after"] = source_hashes_after
    manifest["provider_calls"] = sum(int(entry.get("provider_call_count", 0)) for entry in manifest["chunks"].values())
    manifest["bars_written_this_run"] = total_bars
    manifest["orders_sent"] = 0
    _atomic_write_json(manifest_path, manifest)
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
