"""Read-only integrity, continuity and spread audit for local MT5 CSV files."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from src.spread_quality import audit_spread_timeline
from src.history_data_integrity import CANONICAL_HASH_POLICY, content_hashes, file_content_hashes


SYMBOLS = ("EURUSD", "USDCAD", "AUDUSD")
TIMEFRAMES_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
REQUIRED_COLUMNS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
OHLC_COLUMNS = ("open", "high", "low", "close")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def parse_utc_timestamp(raw: str) -> datetime:
    if not raw:
        raise ValueError("TIMESTAMP_MISSING")
    value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMESTAMP_TIMEZONE_MISSING")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("TIMESTAMP_NOT_UTC")
    return value.astimezone(timezone.utc)


def _number(row: dict[str, str], key: str) -> float:
    raw = row.get(key)
    if raw is None or str(raw).strip() == "":
        raise ValueError(f"FIELD_MISSING_{key.upper()}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"FIELD_NONFINITE_{key.upper()}")
    return value


def _payload_signature(row: dict[str, str]) -> tuple[str | None, ...]:
    return tuple(row.get(key) for key in REQUIRED_COLUMNS[1:])


def _material_signature(row: dict[str, Any]) -> tuple[str, ...]:
    """Compare numeric CSV fields by value, not by display precision."""
    values: list[str] = []
    for key in REQUIRED_COLUMNS[1:]:
        try:
            if key in OHLC_COLUMNS:
                # MT5 exposes prices as binary floats. Compare at 12 significant
                # digits to remove representation noise while retaining precision
                # far below normal FX quote increments. Hashes remain byte-exact.
                value = Decimal(format(float(row.get(key)), ".12g")).normalize()
            else:
                value = Decimal(str(row.get(key))).normalize()
            values.append(format(value, "f"))
        except (InvalidOperation, TypeError, ValueError):
            values.append(str(row.get(key)))
    return tuple(values)


def _load_csv(path: Path, timeframe: str, as_of_utc: datetime) -> dict[str, Any]:
    raw_bytes = path.read_bytes()
    records: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    invalid_timestamp_count = 0
    malformed_row_count = 0
    missing_required_fields: dict[str, int] = {key: 0 for key in REQUIRED_COLUMNS}
    invalid_ohlc_count = 0
    invalid_volume_count = 0
    open_or_future_bar_count = 0
    non_utc_timestamp_count = 0
    out_of_order_count = 0
    timeframe_grid_misalignment_count = 0
    previous_time: datetime | None = None
    duplicates: dict[datetime, tuple[str | None, ...]] = {}
    duplicate_identical_count = 0
    duplicate_conflict_count = 0
    spread_timeline: list[tuple[datetime, Any]] = []
    seconds = TIMEFRAMES_SECONDS[timeframe]
    fields: list[str] = []

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                malformed_row_count += 1
                issues.append({"row": row_number, "code": "CSV_EXTRA_FIELDS"})
                continue
            for key in REQUIRED_COLUMNS:
                if key not in row or row.get(key) is None or str(row.get(key, "")).strip() == "":
                    missing_required_fields[key] += 1
            try:
                timestamp = parse_utc_timestamp(row.get("time", ""))
            except (ValueError, TypeError, OverflowError) as exc:
                invalid_timestamp_count += 1
                code = str(exc) if str(exc).startswith("TIMESTAMP_") else "TIMESTAMP_INVALID"
                if code == "TIMESTAMP_NOT_UTC":
                    non_utc_timestamp_count += 1
                if len(issues) < 50:
                    issues.append({"row": row_number, "code": code})
                continue

            if previous_time is not None and timestamp <= previous_time:
                out_of_order_count += 1
            previous_time = timestamp

            signature = _payload_signature(row)
            if timestamp in duplicates:
                if duplicates[timestamp] == signature:
                    duplicate_identical_count += 1
                else:
                    duplicate_conflict_count += 1
                    if len(issues) < 50:
                        issues.append({"row": row_number, "code": "DUPLICATE_TIMESTAMP_CONFLICT", "time_utc": timestamp.isoformat()})
            else:
                duplicates[timestamp] = signature

            if timestamp.timestamp() % seconds != 0:
                timeframe_grid_misalignment_count += 1
                if len(issues) < 50:
                    issues.append({"row": row_number, "code": "TIMEFRAME_GRID_MISALIGNMENT", "time_utc": timestamp.isoformat()})

            try:
                o, high, low, close = (_number(row, key) for key in OHLC_COLUMNS)
                if min(o, high, low, close) <= 0 or low > min(o, close) or high < max(o, close) or low > high:
                    invalid_ohlc_count += 1
                    if len(issues) < 50:
                        issues.append({"row": row_number, "code": "OHLC_RELATION_INVALID", "time_utc": timestamp.isoformat()})
            except (ValueError, TypeError, OverflowError):
                invalid_ohlc_count += 1
                if len(issues) < 50:
                    issues.append({"row": row_number, "code": "OHLC_FIELD_INVALID", "time_utc": timestamp.isoformat()})

            try:
                volume = _number(row, "tick_volume")
                real_volume = _number(row, "real_volume")
                if volume < 0 or real_volume < 0:
                    raise ValueError("NEGATIVE_VOLUME")
            except (ValueError, TypeError, OverflowError):
                invalid_volume_count += 1

            if timestamp + timedelta(seconds=seconds) > as_of_utc:
                open_or_future_bar_count += 1

            spread_timeline.append((timestamp, row.get("spread")))
            records.append({"time": timestamp, "row": row, "signature": signature, "row_number": row_number})

    unique_records: dict[datetime, dict[str, Any]] = {}
    for record in records:
        unique_records.setdefault(record["time"], record)
    sorted_times = sorted(unique_records)
    ordered = [unique_records[t] for t in sorted_times]
    gaps = [gap for left, right in zip(ordered, ordered[1:]) if (gap := _gap_record(left, right, seconds)) is not None]

    if not fields:
        issues.append({"code": "CSV_HEADER_MISSING"})
    missing_column_names = [key for key in REQUIRED_COLUMNS if key not in fields]
    ohlc_required_columns = ("time", *OHLC_COLUMNS)
    missing_ohlc_column_names = [key for key in ohlc_required_columns if key not in fields]
    missing_ohlc_value_count = sum(missing_required_fields.get(key, 0) for key in ohlc_required_columns)
    integrity_ok = (
        bool(ordered)
        and not missing_ohlc_column_names
        and missing_ohlc_value_count == 0
        and invalid_timestamp_count == 0
        and malformed_row_count == 0
        and duplicate_identical_count == 0
        and duplicate_conflict_count == 0
        and invalid_ohlc_count == 0
        and open_or_future_bar_count == 0
        and non_utc_timestamp_count == 0
        and out_of_order_count == 0
        and timeframe_grid_misalignment_count == 0
    )

    return {
        "path": path.as_posix(),
        "sha256_raw": sha256_bytes(raw_bytes),
        "bytes": len(raw_bytes),
        "columns": fields,
        "missing_column_names": missing_column_names,
        "missing_ohlc_column_names": missing_ohlc_column_names,
        "row_count": len(records),
        "unique_timestamp_count": len(sorted_times),
        "first_utc": sorted_times[0].isoformat() if sorted_times else None,
        "last_utc": sorted_times[-1].isoformat() if sorted_times else None,
        "invalid_timestamp_count": invalid_timestamp_count,
        "non_utc_timestamp_count": non_utc_timestamp_count,
        "malformed_row_count": malformed_row_count,
        "missing_required_field_counts": missing_required_fields,
        "out_of_order_count": out_of_order_count,
        "duplicate_identical_count": duplicate_identical_count,
        "duplicate_conflict_count": duplicate_conflict_count,
        "invalid_ohlc_count": invalid_ohlc_count,
        "invalid_volume_count": invalid_volume_count,
        "timeframe_grid_misalignment_count": timeframe_grid_misalignment_count,
        "open_or_future_bar_count": open_or_future_bar_count,
        "timeframe_seconds": seconds,
        "ohlc_integrity_status": "PASS" if integrity_ok else "FAIL",
        "history_gap_intervals": gaps,
        "history_gap_interval_count": len(gaps),
        "spread_audit": audit_spread_timeline(spread_timeline),
        "issues_sample": issues[:50],
        "_records": ordered,
    }


def _gap_bucket(timestamp: datetime) -> str:
    weekday = timestamp.weekday()
    if weekday == 5:
        return "EXPECTED_WEEKEND_CLOSED_SATURDAY"
    if weekday == 6:
        return "PENDING_WEEKLY_SESSION_SUNDAY"
    if weekday == 4:
        return "PENDING_WEEKLY_CLOSE_FRIDAY"
    return "PENDING_TRADING_CALENDAR_OR_DATA_GAP"


def _compress_slots(slots: list[datetime], seconds: int) -> list[dict[str, Any]]:
    if not slots:
        return []
    ranges: list[dict[str, Any]] = []
    start = previous = slots[0]
    bucket = _gap_bucket(start)
    count = 1
    for timestamp in slots[1:]:
        current_bucket = _gap_bucket(timestamp)
        if current_bucket == bucket and timestamp - previous == timedelta(seconds=seconds):
            previous = timestamp
            count += 1
            continue
        ranges.append({"start_utc": start.isoformat(), "end_utc": previous.isoformat(), "missing_bar_count": count, "classification": bucket})
        start = previous = timestamp
        bucket = current_bucket
        count = 1
    ranges.append({"start_utc": start.isoformat(), "end_utc": previous.isoformat(), "missing_bar_count": count, "classification": bucket})
    return ranges


def _gap_record(left: dict[str, Any], right: dict[str, Any], seconds: int) -> dict[str, Any] | None:
    before = left["time"]
    after = right["time"]
    delta = int((after - before).total_seconds())
    if delta <= seconds:
        return None
    if delta % seconds:
        return {
            "previous_bar_utc": before.isoformat(),
            "next_bar_utc": after.isoformat(),
            "missing_start_utc": None,
            "missing_end_utc": None,
            "missing_bar_count": None,
            "elapsed_seconds": delta,
            "classification": "PENDING_IRREGULAR_TIMEFRAME_CADENCE",
            "classification_ranges": [],
            # There is no defensible set of missing bar opens to request when
            # the observations are not aligned to an integer number of bars.
            "recovery_candidate": False,
            "reason": "OBSERVED_TIMES_NOT_AN_INTEGER_NUMBER_OF_TIMEFRAME_STEPS_APART",
        }
    slots: list[datetime] = []
    current = before + timedelta(seconds=seconds)
    while current < after:
        slots.append(current)
        current += timedelta(seconds=seconds)
    compressed = _compress_slots(slots, seconds)
    pending_slots = sum(item["missing_bar_count"] for item in compressed if not item["classification"].startswith("EXPECTED_WEEKEND"))
    weekday_slots = sum(item["missing_bar_count"] for item in compressed if item["classification"] == "PENDING_TRADING_CALENDAR_OR_DATA_GAP")
    recovery_candidate = weekday_slots > 0
    status = "PENDING_MARKET_CALENDAR" if pending_slots else "WEEKEND_CLOSURE_CANDIDATE_ONLY"
    return {
        "previous_bar_utc": before.isoformat(),
        "next_bar_utc": after.isoformat(),
        "missing_start_utc": slots[0].isoformat() if slots else None,
        "missing_end_utc": slots[-1].isoformat() if slots else None,
        "missing_bar_count": len(slots),
        "pending_bar_count": pending_slots,
        "weekday_calendar_or_data_gap_bar_count": weekday_slots,
        "elapsed_seconds": delta,
        "classification": status,
        "classification_ranges": compressed,
        "recovery_candidate": recovery_candidate,
        "reason": "WEEKEND_AND_MARKET_HOURS_NOT_ASSUMED" if pending_slots else "MISSING_SLOTS_FALL_ONLY_ON_SATURDAY_UTC",
    }


def _manifest_hash_entry(manifest: dict[str, Any], symbol: str, timeframe: str) -> str | None:
    try:
        return manifest["symbols"][symbol][timeframe]["sha256"]
    except (KeyError, TypeError):
        return None


def _hash_integrity(path: Path, expected: str | None) -> dict[str, Any]:
    raw = path.read_bytes()
    hashes = content_hashes(raw)
    raw_hash = hashes["raw_sha256"]
    canonical_hash = hashes["canonical_sha256"]
    # The frozen 2026 snapshot manifest predates hash policy v1 and contains
    # the CRLF byte digest. Accept only the exact CRLF/LF transformation.
    legacy_crlf_hash = sha256_bytes(canonicalize_crlf(raw))
    if expected is None:
        status = "NO_MANIFEST_HASH"
    elif raw_hash == expected:
        status = "PASS_RAW_HASH_MATCH"
    elif canonical_hash == expected:
        status = "PASS_CANONICAL_LF_HASH_MATCH"
    elif legacy_crlf_hash == expected:
        status = "PASS_LEGACY_CRLF_LINE_ENDING_MATCH"
    else:
        status = "FAIL_HASH_MISMATCH"
    return {
        "actual_sha256_raw": raw_hash,
        "manifest_sha256": expected,
        "actual_canonical_sha256_lf": canonical_hash,
        "legacy_crlf_variant_sha256": legacy_crlf_hash,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "raw_hash_matches_manifest": expected is not None and raw_hash == expected,
        "canonical_hash_matches_manifest": expected is not None and canonical_hash == expected,
        "legacy_crlf_variant_matches_manifest": expected is not None and legacy_crlf_hash == expected,
        "status": status,
    }


def canonicalize_crlf(raw: bytes) -> bytes:
    """Legacy manifest compatibility: encode only LF/CRLF line-ending changes."""
    normalized = raw.replace(b"\r\n", b"\n")
    if b"\r" in normalized:
        raise ValueError("CSV_CANONICALIZATION_UNSUPPORTED_LONE_CR")
    return normalized.replace(b"\n", b"\r\n")


def _compare_overlap(old: list[dict[str, Any]], recent: list[dict[str, Any]]) -> dict[str, Any]:
    old_map = {record["time"]: record for record in old}
    recent_map = {record["time"]: record for record in recent}
    shared = sorted(set(old_map) & set(recent_map))
    price_conflicts: list[dict[str, Any]] = []
    spread_conflicts: list[dict[str, Any]] = []
    for timestamp in shared:
        a, b = old_map[timestamp]["row"], recent_map[timestamp]["row"]
        unequal_ohlc = []
        for column in OHLC_COLUMNS:
            try:
                equal = Decimal(a[column]) == Decimal(b[column])
            except (InvalidOperation, TypeError):
                equal = a.get(column) == b.get(column)
            if not equal:
                unequal_ohlc.append(column)
        if unequal_ohlc and len(price_conflicts) < 100:
            price_conflicts.append({"time_utc": timestamp.isoformat(), "different_price_fields": unequal_ohlc, "old": {k: a.get(k) for k in OHLC_COLUMNS}, "recent": {k: b.get(k) for k in OHLC_COLUMNS}})
        if a.get("spread") != b.get("spread") and len(spread_conflicts) < 100:
            spread_conflicts.append({"time_utc": timestamp.isoformat(), "old_spread": a.get("spread"), "recent_spread": b.get("spread")})
    return {
        "shared_timestamp_count": len(shared),
        "price_conflict_count": len(price_conflicts),
        "price_conflicts_sample": price_conflicts,
        "spread_value_conflict_count": len(spread_conflicts),
        "spread_value_conflicts_sample": spread_conflicts,
        "price_comparison_status": "NO_OVERLAP_TO_COMPARE" if not shared else ("CONFLICTS_FOUND_RETAIN_BOTH" if price_conflicts else "PASS_ALL_OVERLAPPING_OHLC_MATCH"),
    }


def _public_source(source: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in source.items() if key != "_records"}


def _merge_record_variants(
    old: list[dict[str, Any]],
    recent: list[dict[str, Any]],
    backfill: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    variants: dict[datetime, dict[tuple[str | None, ...], dict[str, Any]]] = {}
    sources = [("data/market", old), ("mt5_real_readonly", recent)]
    if backfill:
        sources.append(("backfill_run", backfill))
    for source_name, records in sources:
        for record in records:
            signature = _material_signature(record["row"])
            variant = variants.setdefault(record["time"], {}).setdefault(signature, {**record, "sources": []})
            source_label = record.get("source", source_name)
            if source_label not in variant["sources"]:
                variant["sources"].append(source_label)
    merged = []
    for timestamp in sorted(variants):
        for variant in variants[timestamp].values():
            merged.append(variant)
    return merged


def audit_market_history(
    root: Path,
    *,
    as_of_utc: datetime,
    backfill_run: str | None = None,
    symbols: tuple[str, ...] = SYMBOLS,
    timeframes_seconds: dict[str, int] = TIMEFRAMES_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Audit original sources and, optionally, one explicitly selected backfill run."""
    if as_of_utc.tzinfo is None or as_of_utc.utcoffset() is None:
        raise ValueError("AS_OF_TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    as_of_utc = as_of_utc.astimezone(timezone.utc)
    snapshot = root / "data/market_history/mt5_real_readonly/20261109T202112.171333Z"
    old_root = root / "data/market"
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    manifest_digest = sha256_bytes(manifest_path.read_bytes()) if manifest_path.is_file() else None

    gap_entries: dict[str, Any] = {}
    spread_entries: dict[str, Any] = {}
    all_source_hashes_before: dict[str, str] = {}
    all_source_hashes_after: dict[str, str] = {}
    bridge_recovery_candidates: list[dict[str, Any]] = []
    calendar_review_items: list[dict[str, Any]] = []
    run_validation: dict[str, Any] | None = None
    if backfill_run is not None:
        from src.market_history_backfill import validate_backfill_run

        run_validation = validate_backfill_run(root, backfill_run, as_of_utc=as_of_utc)
    expected_snapshot_files = {f"{symbol}_{timeframe}.csv" for symbol in symbols for timeframe in timeframes_seconds}
    actual_snapshot_files = {path.name for path in snapshot.glob("*.csv")}
    manifest_missing_files = sorted(expected_snapshot_files - actual_snapshot_files)
    extra_snapshot_files = sorted(actual_snapshot_files - expected_snapshot_files)
    expected_old_files = {f"{symbol}/{timeframe}.csv" for symbol in symbols for timeframe in timeframes_seconds}
    actual_old_files = {
        path.relative_to(old_root).as_posix()
        for symbol in symbols
        for path in (old_root / symbol).glob("*.csv")
    }
    extra_old_files = sorted(actual_old_files - expected_old_files)
    manifest_file_hash_results: dict[str, Any] = {}
    critical_integrity_errors: list[str] = []
    if manifest_missing_files:
        critical_integrity_errors.append("SNAPSHOT_SOURCE_FILES_MISSING")
    if extra_snapshot_files:
        critical_integrity_errors.append("SNAPSHOT_SOURCE_FILES_UNEXPECTED")
    if extra_old_files:
        critical_integrity_errors.append("PREVIOUS_SOURCE_FILES_UNEXPECTED")
    combined_source_rows = 0

    for symbol in symbols:
        gap_entries[symbol] = {}
        spread_entries[symbol] = {}
        for timeframe, seconds in timeframes_seconds.items():
            old_path = old_root / symbol / f"{timeframe}.csv"
            recent_path = snapshot / f"{symbol}_{timeframe}.csv"
            sources: dict[str, Any] = {}
            for label, path in (("previous", old_path), ("recent", recent_path)):
                if not path.is_file():
                    sources[label] = {"path": path.as_posix(), "status": "MISSING_FILE"}
                    critical_integrity_errors.append(f"MISSING_SOURCE:{path.as_posix()}")
                    continue
                result = _load_csv(path, timeframe, as_of_utc)
                sources[label] = result
                relative_path = path.relative_to(root).as_posix()
                result["path"] = relative_path
                all_source_hashes_before[relative_path] = result["sha256_raw"]
                combined_source_rows += result["row_count"]
                if result["ohlc_integrity_status"] != "PASS":
                    critical_integrity_errors.append(f"OHLC_INTEGRITY:{path.as_posix()}")

            old_result, recent_result = sources.get("previous", {}), sources.get("recent", {})
            old_records, recent_records = old_result.get("_records", []), recent_result.get("_records", [])
            recent_hash = _hash_integrity(recent_path, _manifest_hash_entry(manifest, symbol, timeframe)) if recent_path.is_file() else {}
            manifest_entry = None
            try:
                manifest_entry = manifest["symbols"][symbol][timeframe]
            except (KeyError, TypeError):
                pass
            if manifest_entry is not None:
                recent_hash["row_count_matches_manifest"] = recent_result.get("row_count") == manifest_entry.get("new_bars")
                recent_hash["latest_closed_matches_manifest"] = recent_result.get("last_utc") == manifest_entry.get("latest_closed_utc")
                recent_hash["manifest_metadata_matches"] = recent_hash["row_count_matches_manifest"] and recent_hash["latest_closed_matches_manifest"]
                if not recent_hash["manifest_metadata_matches"]:
                    recent_hash["status"] = "FAIL_MANIFEST_METADATA_MISMATCH"
                    critical_integrity_errors.append(f"MANIFEST_METADATA_MISMATCH:{recent_path.as_posix()}")
            else:
                recent_hash["status"] = "FAIL_MANIFEST_ENTRY_MISSING"
                critical_integrity_errors.append(f"MANIFEST_ENTRY_MISSING:{recent_path.as_posix()}")
            manifest_file_hash_results[f"{symbol}_{timeframe}"] = recent_hash
            if recent_hash.get("status") == "FAIL_HASH_MISMATCH":
                critical_integrity_errors.append(f"MANIFEST_HASH_MISMATCH:{recent_path.as_posix()}")

            overlap = _compare_overlap(old_records, recent_records)
            if overlap["price_conflict_count"]:
                critical_integrity_errors.append(f"OVERLAP_PRICE_CONFLICT:{symbol}:{timeframe}")
            backfill_records = []
            if run_validation is not None:
                backfill_records = run_validation.get("records_by_symbol_timeframe", {}).get(f"{symbol}|{timeframe}", [])
                combined_source_rows += len(backfill_records)
            merged_records = _merge_record_variants(old_records, recent_records, backfill_records)
            combined_gaps = [gap for left, right in zip(merged_records, merged_records[1:]) if left["time"] != right["time"] and (gap := _gap_record(left, right, seconds)) is not None]

            bridge = None
            if old_records and recent_records:
                old_last = old_records[-1]["time"]
                recent_first = recent_records[0]["time"]
                if old_last < recent_first:
                    bridge = _gap_record(old_records[-1], recent_records[0], seconds)
                    if bridge is None:
                        bridge = {
                            "status": "CONTINUOUS_NO_MISSING_CADENCE_SLOTS",
                            "previous_bar_utc": old_last.isoformat(),
                            "next_bar_utc": recent_first.isoformat(),
                            "missing_bar_count": 0,
                        }
                elif recent_first <= old_last:
                    bridge = {"status": "OVERLAP", **overlap}
            for gap in combined_gaps:
                if gap.get("recovery_candidate"):
                    bridge_recovery_candidates.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "interval_id": hashlib.sha256(f"{symbol}|{timeframe}|{gap['previous_bar_utc']}|{gap['next_bar_utc']}".encode()).hexdigest()[:16],
                        "previous_bar_utc": gap["previous_bar_utc"],
                        "next_bar_utc": gap["next_bar_utc"],
                        "missing_start_utc": gap["missing_start_utc"],
                        "missing_end_utc": gap["missing_end_utc"],
                        "missing_bar_count": gap["missing_bar_count"],
                        "reason": "EXACT_ALIGNED_MISSING_CADENCE_SLOTS_WITH_SESSION_STATUS_PENDING_VERIFICATION",
                    })
                elif gap.get("classification") in {"PENDING_MARKET_CALENDAR", "PENDING_IRREGULAR_TIMEFRAME_CADENCE"}:
                    calendar_review_items.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "previous_bar_utc": gap["previous_bar_utc"],
                        "next_bar_utc": gap["next_bar_utc"],
                        "missing_start_utc": gap["missing_start_utc"],
                        "missing_end_utc": gap["missing_end_utc"],
                        "classification": gap["classification"],
                        "reason": gap["reason"],
                        "classification_ranges": gap["classification_ranges"],
                    })

            gap_entries[symbol][timeframe] = {
                "timeframe_seconds": seconds,
                "previous_source": _public_source(old_result) if old_result else sources.get("previous"),
                "recent_source": _public_source(recent_result) if recent_result else sources.get("recent"),
                "transition": {
                    "previous_last_utc": old_result.get("last_utc"),
                    "recent_first_utc": recent_result.get("first_utc"),
                    "bridge": bridge,
                    "overlap": overlap,
                    "source_price_conflicts_preserved": overlap["price_conflict_count"],
                    "backfill_row_count": len(backfill_records),
                    "backfill_source_conflicts": [
                        conflict for conflict in (run_validation or {}).get("source_conflicts", [])
                        if conflict.get("symbol") == symbol and conflict.get("timeframe") == timeframe
                    ],
                    "combined_gap_intervals": combined_gaps,
                    "combined_gap_interval_count": len(combined_gaps),
                },
            }

            union_values: dict[datetime, set[str]] = {}
            union_spread: dict[datetime, Any] = {}
            for record in merged_records:
                timestamp = record["time"]
                spread_value = record["row"].get("spread")
                union_values.setdefault(timestamp, set()).add(str(spread_value))
                union_spread.setdefault(timestamp, spread_value)
            union_times = [(timestamp, union_spread[timestamp] if len(values) == 1 else None) for timestamp, values in union_values.items()]
            spread_entries[symbol][timeframe] = {
                "previous_source": old_result.get("spread_audit"),
                "recent_source": recent_result.get("spread_audit"),
                "combined_timestamp_union": audit_spread_timeline(sorted(union_times, key=lambda item: item[0])),
                "execution_inference_eligible": False,
                "execution_inference_reason": "UNVERIFIED_ZERO_SPREADS_OR_BROKER_COST_COVERAGE_INCOMPLETE",
                "technical_research_eligible": bool(
                    old_result.get("ohlc_integrity_status") == "PASS"
                    and recent_result.get("ohlc_integrity_status") == "PASS"
                    and not any(
                        conflict.get("symbol") == symbol and conflict.get("timeframe") == timeframe
                        for conflict in (run_validation or {}).get("source_conflicts", [])
                    )
                ),
                "technical_research_restrictions": [
                    "Do not calculate indicators across unresolved history gaps.",
                    "Use only windows with valid closed OHLC bars and record data source provenance.",
                ],
            }

    for path_text, expected in all_source_hashes_before.items():
        try:
            all_source_hashes_after[path_text] = sha256_bytes((root / path_text).read_bytes())
        except OSError:
            all_source_hashes_after[path_text] = "MISSING_AFTER_AUDIT"
    originals_unchanged = all_source_hashes_before == all_source_hashes_after
    data_files_match = (
        not manifest_missing_files
        and not extra_snapshot_files
        and not extra_old_files
        and len(actual_snapshot_files) == len(expected_snapshot_files)
    )
    if run_validation is None:
        continuity_status = "HISTORY_CONTINUITY_NOT_AUDITED"
    elif run_validation.get("integrity_status") == "BACKFILL_INTEGRITY_FAILED" or critical_integrity_errors:
        continuity_status = "HISTORY_CONTINUITY_BLOCKED"
        critical_integrity_errors.extend(run_validation.get("errors", []))
    elif run_validation.get("source_conflicts"):
        continuity_status = "HISTORY_CONTINUITY_BLOCKED"
    elif bridge_recovery_candidates or calendar_review_items or run_validation.get("pending_chunk_ids"):
        continuity_status = "HISTORY_CONTINUITY_PARTIAL"
    else:
        continuity_status = "HISTORY_CONTINUITY_VERIFIED"

    from src.market_history_backfill import _source_hashes

    portable_source_hashes = _source_hashes(root, symbols=symbols, timeframes=timeframes_seconds)
    no_integrity_errors = not critical_integrity_errors
    audit_status = "PASS_WITH_PENDING_GAPS" if no_integrity_errors and originals_unchanged and data_files_match else "FAIL"
    if continuity_status == "HISTORY_CONTINUITY_BLOCKED":
        audit_status = "FAIL"

    gaps_report = {
        "schema_version": 2,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "audit_as_of_utc": as_of_utc.isoformat(),
        "status": audit_status,
        "history_continuity_status": continuity_status,
        "backfill_run": None if run_validation is None else {
            key: value for key, value in run_validation.items() if key != "records_by_symbol_timeframe"
        },
        "base_snapshot": "data/market_history/mt5_real_readonly/20261109T202112.171333Z",
        "previous_source": "data/market",
        "timeframe_grid_policy": "UTC fixed-duration steps; missing bars are candidate slots, not automatic market-data errors",
        "market_calendar_policy": {
            "saturday_utc": "EXPECTED_WEEKEND_CLOSED_CANDIDATE",
            "friday_utc": "PENDING_WEEKLY_CLOSE_HOURS",
            "sunday_utc": "PENDING_WEEKLY_OPEN_HOURS",
            "monday_to_thursday_utc": "PENDING_TRADING_CALENDAR_OR_DATA_GAP",
            "note": "No broker holiday or DST session calendar is assumed. Friday/Sunday boundary slots remain pending until verified.",
        },
        "snapshot_file_inventory": {
            "expected_csv_count": len(expected_snapshot_files),
            "actual_csv_count": len(actual_snapshot_files),
            "missing": manifest_missing_files,
            "extra": extra_snapshot_files,
            "previous_source_extra_csv_files": extra_old_files,
            "manifest_sha256": manifest_digest,
            "manifest_file_hash_validation": manifest_file_hash_results,
        },
        "source_files_unchanged": originals_unchanged,
        "source_hashes_before": all_source_hashes_before,
        "source_hashes_after": all_source_hashes_after,
        "critical_integrity_errors": critical_integrity_errors,
        "symbols": gap_entries,
        "counts": {
            "all_source_rows_audited": combined_source_rows,
            "pending_recovery_intervals": len(bridge_recovery_candidates),
            "calendar_review_intervals": len(calendar_review_items),
            "price_overlap_conflicts": sum(data[tf]["transition"]["overlap"]["price_conflict_count"] for data in gap_entries.values() for tf in timeframes_seconds),
            "backfill_rows_in_union": 0 if run_validation is None else run_validation.get("row_count", 0),
            "backfill_integrity_errors": 0 if run_validation is None else len(run_validation.get("errors", [])),
            "backfill_source_conflicts": 0 if run_validation is None else len(run_validation.get("source_conflicts", [])),
        },
        "pending_recovery_intervals": bridge_recovery_candidates,
        "calendar_review_intervals": calendar_review_items,
        "no_csv_source_was_written": True,
    }

    spread_report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS_DIAGNOSTIC_EXECUTION_QUALITY_NOT_CONFIRMATORY",
        "raw_field": "MT5 rates spread field",
        "units": "MT5 points; no conversion to price or pips",
        "quality_states": ["OBSERVED_VALID", "OBSERVED_ZERO_UNVERIFIED", "MISSING", "INVALID", "IMPUTED_ASSUMPTION"],
        "classification_policy": "Positive finite values are OBSERVED_VALID; zero is OBSERVED_ZERO_UNVERIFIED; missing or malformed values are not imputed.",
        "current_mt5_readonly_snapshot": _safe_current_spread_snapshot(root),
        "current_snapshot_not_extrapolated_to_history": True,
        "source_values_modified": False,
        "by_symbol_timeframe": spread_entries,
        "execution_eligibility_policy": {
            "confirmatory_economic_research": "NOT_ELIGIBLE when zero spreads are unverified, required spreads are missing, or verified broker commission/slippage coverage is absent.",
            "technical_research": "Eligible only within OHLC-integrity-pass windows; unresolved temporal gaps must not be bridged by indicators.",
        },
    }

    plan = {
        "schema_version": 2,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "symbols": list(symbols),
        "timeframes_seconds": dict(timeframes_seconds),
        "status": "HISTORY_BACKFILL_PREPARED",
        "prepared_utc": datetime.now(timezone.utc).isoformat(),
        "source_audit": "reports/phase2_2_2/history_gap_audit.json",
        "live_download_performed": False,
        "connection_to_mt5": False,
        "orders_sent": 0,
        "provider_calls": 0,
        "chunk_policy": {"max_bars_per_request": 1000, "overlap_bars_each_side": 2},
        "backfill_intervals": bridge_recovery_candidates,
        "calendar_review_before_download": calendar_review_items,
        "verification_requirements": [
            "Confirm terminal tradeapi_disabled is exactly true before and after each copy_rates_range call.",
            "Confirm account trade_mode is REAL; refuse DEMO, unavailable, or unverified accounts.",
            "Fetch with bounded UTC copy_rates_range intervals and at most 1000 requested cadence bars including overlaps.",
            "Exclude bars whose close time is after the UTC fetch time.",
            "Validate timestamps, OHLC, nonnegative spread/volume and duplicates.",
            "Preserve original files and any source/download price conflicts; never choose a conflict silently.",
            "Write only beneath data/market_history/backfill_runs/<run-id>/ and hash every output.",
            "Resume only verified completed chunks; refuse corrupted manifests or outputs.",
            "Do not claim complete continuity until a fresh audit shows no pending recovery intervals.",
        ],
        "future_command": "python scripts/backfill_market_history.py --dry-run",
        "future_explicit_download_command": "python scripts/backfill_market_history.py --download --confirm-real-read-only",
        "download_command_executed_in_this_phase": False,
        "source_file_hashes": portable_source_hashes,
        "source_file_count": len(portable_source_hashes),
    }
    return gaps_report, spread_report, plan


def _safe_current_spread_snapshot(root: Path) -> dict[str, Any]:
    path = root / "reports/mt5_real_readonly_summary.json"
    if not path.is_file():
        return {"status": "UNAVAILABLE", "source": path.as_posix(), "values_used_for_historical_inference": False}
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
        values = {}
        for symbol in SYMBOLS:
            item = source.get("symbols", {}).get(symbol, {})
            values[symbol] = {"spread_points": item.get("spread_points"), "spread_quality": item.get("spread_quality")}
        return {
            "status": source.get("status", "UNKNOWN"),
            "generated_utc": source.get("generated_utc"),
            "tradeapi_disabled": source.get("read_only_safety", {}).get("tradeapi_disabled"),
            "values_by_symbol": values,
            "source": path.as_posix(),
            "values_used_for_historical_inference": False,
        }
    except (OSError, ValueError, TypeError):
        return {"status": "INVALID_PUBLIC_SUMMARY", "source": path.as_posix(), "values_used_for_historical_inference": False}
