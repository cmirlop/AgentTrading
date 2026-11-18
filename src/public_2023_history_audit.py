"""Streaming integrity and provenance audit for the public 2023+ market CSVs.

This module reads only the fifteen tracked ``data/market/{symbol}/{tf}.csv``
files and the phase 2.2.2g public snapshot report. It never connects to MT5,
classifies broker closures, writes market data, or creates trading signals.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

from config.market_config import SYMBOLS, TIMEFRAMES
from src.history_data_integrity import CANONICAL_HASH_POLICY, ensure_no_symlink_path
from src.market_history_audit import REQUIRED_COLUMNS, parse_utc_timestamp
from src.verified_history_scan import validate_history_source_manifest


TARGET_START_UTC = datetime(2023, 1, 1, tzinfo=timezone.utc)
EXPECTED_2023_PLUS_ROWS = 1_252_070
EXPECTED_FROZEN_SCAN_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
PHASE_G_REPORT = "reports/phase2_2_2g/validation_summary.json"
FROZEN_SCAN_PATH = "data/backtest/technical_candidate_scan.csv"
D1_CLOSE_GRACE = timedelta(hours=26)
HASH_CHUNK_SIZE = 1024 * 1024
MAX_ISSUE_SAMPLES = 50
MAX_SPREAD_DISTINCT = 10_000


def _json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _streaming_file_hashes(path: Path) -> dict[str, Any]:
    """Hash exact bytes plus LF-canonical and CRLF-equivalent bytes in chunks."""
    raw_hash = hashlib.sha256()
    lf_hash = hashlib.sha256()
    crlf_hash = hashlib.sha256()
    carry = b""
    lone_cr_count = 0
    byte_count = 0
    with path.open("rb") as handle:
        while block := handle.read(HASH_CHUNK_SIZE):
            byte_count += len(block)
            raw_hash.update(block)
            data = carry + block
            carry = b"\r" if data.endswith(b"\r") else b""
            if carry:
                data = data[:-1]
            normalized = data.replace(b"\r\n", b"\n")
            lone_cr_count += normalized.count(b"\r")
            lf_hash.update(normalized)
            crlf_hash.update(normalized.replace(b"\n", b"\r\n"))
    if carry:
        lone_cr_count += 1
        lf_hash.update(carry)
        crlf_hash.update(carry)
    return {
        "raw_sha256": raw_hash.hexdigest(),
        "canonical_sha256": lf_hash.hexdigest(),
        "crlf_variant_sha256": crlf_hash.hexdigest(),
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "byte_count": byte_count,
        "lone_cr_count": lone_cr_count,
    }


def _phase_g_hash_match(expected_raw_sha256: str, hashes: dict[str, Any]) -> str:
    if hashes["raw_sha256"] == expected_raw_sha256:
        return "EXACT_RAW_MATCH"
    if hashes["canonical_sha256"] == expected_raw_sha256:
        return "LF_EOL_EQUIVALENT"
    if hashes["crlf_variant_sha256"] == expected_raw_sha256:
        return "CRLF_EOL_EQUIVALENT"
    return "MISMATCH"


def _decimal(raw: str) -> Decimal:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("NUMERIC_FIELD_MISSING")
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise ValueError("NUMERIC_FIELD_INVALID") from None
    if not value.is_finite():
        raise ValueError("NUMERIC_FIELD_NONFINITE")
    return value


def _duration_percentiles(histogram: dict[int, int]) -> dict[str, int | None]:
    count = sum(histogram.values())
    if not count:
        return {"p50_nearest_rank_seconds": None, "p90_nearest_rank_seconds": None,
                "p95_nearest_rank_seconds": None, "p99_nearest_rank_seconds": None}

    def rank_value(percent: int) -> int:
        rank = max(1, math.ceil(count * percent / 100))
        cumulative = 0
        for seconds, frequency in sorted(histogram.items()):
            cumulative += frequency
            if cumulative >= rank:
                return seconds
        return max(histogram)

    return {
        "p50_nearest_rank_seconds": rank_value(50),
        "p90_nearest_rank_seconds": rank_value(90),
        "p95_nearest_rank_seconds": rank_value(95),
        "p99_nearest_rank_seconds": rank_value(99),
    }


def _spread_percentile(histogram: dict[Decimal, int], percentile: int) -> str | None:
    count = sum(histogram.values())
    if not count:
        return None
    rank = max(1, math.ceil(count * percentile / 100))
    cumulative = 0
    for value, frequency in sorted(histogram.items()):
        cumulative += frequency
        if cumulative >= rank:
            return format(value, "f")
    return format(max(histogram), "f")


def _audit_csv(path: Path, *, symbol: str, timeframe: str, as_of_utc: datetime,
               cutoff_utc: datetime, expected: dict[str, Any]) -> dict[str, Any]:
    hashes_before = _streaming_file_hashes(path)
    seconds = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}[timeframe]
    counts = {
        "rows": 0, "valid_timestamps": 0, "duplicate_timestamps": 0,
        "out_of_order_timestamps": 0, "pre_2023_rows": 0,
        "rows_after_as_of": 0, "rows_after_declared_cutoff": 0,
        "invalid_ohlc_rows": 0, "invalid_volume_rows": 0,
        "invalid_spread_rows": 0, "missing_spread_rows": 0,
        "zero_spread_rows": 0, "positive_spread_rows": 0,
    }
    issues: list[dict[str, Any]] = []
    timestamps_seen: set[int] = set()
    first_time: datetime | None = None
    last_time: datetime | None = None
    previous_time: datetime | None = None
    gap_durations: dict[int, int] = {}
    gap_months: dict[str, int] = {}
    gap_previous_weekdays: dict[str, int] = {}
    gap_next_weekdays: dict[str, int] = {}
    gap_shapes = {
        "WEEKEND_SHAPED_UNKNOWN": 0,
        "SHORT_OR_INTRADAY_UNKNOWN": 0,
        "MULTI_DAY_UNKNOWN": 0,
    }
    spread_histogram: dict[Decimal, int] = {}
    spread_histogram_truncated = False
    header_valid = False

    def issue(row_number: int, code: str, timestamp: datetime | None = None) -> None:
        if len(issues) < MAX_ISSUE_SAMPLES:
            record: dict[str, Any] = {"row": row_number, "code": code}
            if timestamp is not None:
                record["time_utc"] = timestamp.isoformat()
            issues.append(record)

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        header_valid = header == list(REQUIRED_COLUMNS)
        if not header_valid:
            issue(1, "CSV_HEADER_MISMATCH")
        else:
            for row_number, row in enumerate(reader, start=2):
                counts["rows"] += 1
                if not row or all(not field.strip() for field in row):
                    issue(row_number, "CSV_EMPTY_ROW")
                    continue
                if len(row) != len(REQUIRED_COLUMNS):
                    issue(row_number, "CSV_COLUMN_COUNT_MISMATCH")
                    continue
                record = dict(zip(REQUIRED_COLUMNS, row))
                try:
                    stamp = parse_utc_timestamp(record["time"])
                except (ValueError, TypeError, OverflowError):
                    issue(row_number, "TIMESTAMP_INVALID")
                    continue

                counts["valid_timestamps"] += 1
                epoch_microseconds = int(stamp.timestamp() * 1_000_000)
                if epoch_microseconds in timestamps_seen:
                    counts["duplicate_timestamps"] += 1
                    issue(row_number, "DUPLICATE_TIMESTAMP", stamp)
                else:
                    timestamps_seen.add(epoch_microseconds)
                if previous_time is not None:
                    if stamp < previous_time:
                        counts["out_of_order_timestamps"] += 1
                        issue(row_number, "TIMESTAMP_OUT_OF_ORDER", stamp)
                    delta_seconds = int((stamp - previous_time).total_seconds())
                    if delta_seconds > seconds:
                        gap_durations[delta_seconds] = gap_durations.get(delta_seconds, 0) + 1
                        month = previous_time.strftime("%Y-%m")
                        gap_months[month] = gap_months.get(month, 0) + 1
                        previous_day = previous_time.strftime("%A")
                        next_day = stamp.strftime("%A")
                        gap_previous_weekdays[previous_day] = gap_previous_weekdays.get(previous_day, 0) + 1
                        gap_next_weekdays[next_day] = gap_next_weekdays.get(next_day, 0) + 1
                        weekend_shaped = (
                            previous_time.weekday() == 4
                            and stamp.weekday() in {6, 0}
                            and 36 * 3600 <= delta_seconds <= 96 * 3600
                        )
                        if weekend_shaped:
                            gap_shapes["WEEKEND_SHAPED_UNKNOWN"] += 1
                        elif delta_seconds < 36 * 3600:
                            gap_shapes["SHORT_OR_INTRADAY_UNKNOWN"] += 1
                        else:
                            gap_shapes["MULTI_DAY_UNKNOWN"] += 1
                previous_time = stamp
                first_time = stamp if first_time is None else first_time
                last_time = stamp
                if stamp < TARGET_START_UTC:
                    counts["pre_2023_rows"] += 1
                    issue(row_number, "BAR_BEFORE_2023_TARGET", stamp)
                if stamp > as_of_utc:
                    counts["rows_after_as_of"] += 1
                    issue(row_number, "BAR_AFTER_SNAPSHOT_AS_OF", stamp)
                if stamp > cutoff_utc:
                    counts["rows_after_declared_cutoff"] += 1
                    issue(row_number, "BAR_AFTER_DECLARED_CUTOFF", stamp)

                try:
                    o, high, low, close = (_decimal(record[key]) for key in ("open", "high", "low", "close"))
                    if min(o, high, low, close) <= 0 or high < max(o, close) or low > min(o, close) or high < low:
                        raise ValueError("OHLC_RELATION_INVALID")
                except (ValueError, TypeError, OverflowError):
                    counts["invalid_ohlc_rows"] += 1
                    issue(row_number, "OHLC_INVALID", stamp)

                try:
                    tick_volume = _decimal(record["tick_volume"])
                    real_volume = _decimal(record["real_volume"])
                    if tick_volume < 0 or real_volume < 0:
                        raise ValueError("VOLUME_NEGATIVE")
                except (ValueError, TypeError, OverflowError):
                    counts["invalid_volume_rows"] += 1
                    issue(row_number, "VOLUME_INVALID", stamp)

                raw_spread = record["spread"]
                if not raw_spread.strip():
                    counts["missing_spread_rows"] += 1
                    counts["invalid_spread_rows"] += 1
                    issue(row_number, "SPREAD_MISSING", stamp)
                else:
                    try:
                        spread = _decimal(raw_spread)
                        if spread < 0:
                            raise ValueError("SPREAD_NEGATIVE")
                        if spread == 0:
                            counts["zero_spread_rows"] += 1
                        else:
                            counts["positive_spread_rows"] += 1
                            if spread not in spread_histogram and len(spread_histogram) >= MAX_SPREAD_DISTINCT:
                                spread_histogram_truncated = True
                            else:
                                spread_histogram[spread] = spread_histogram.get(spread, 0) + 1
                    except (ValueError, TypeError, OverflowError):
                        counts["invalid_spread_rows"] += 1
                        issue(row_number, "SPREAD_INVALID", stamp)

    declared_raw = expected.get("sha256")
    hash_match = _phase_g_hash_match(declared_raw, hashes_before) if isinstance(declared_raw, str) else "MISSING_DECLARED_HASH"
    expected_rows = expected.get("rows_after_2023plus")
    expected_first = expected.get("first_2023plus_utc")
    expected_last = expected.get("last_closed_utc")
    actual_first_text = first_time.isoformat() if first_time else None
    actual_last_text = last_time.isoformat() if last_time else None
    metadata_matches = (
        counts["rows"] == expected_rows
        and first_time is not None and _iso_utc_equal(actual_first_text, expected_first)
        and last_time is not None and _iso_utc_equal(actual_last_text, expected_last)
        and _iso_utc_equal(actual_last_text, expected.get("effective_cutoff_utc"))
    )
    close_grace = D1_CLOSE_GRACE if timeframe == "D1" else timedelta(seconds=seconds)
    latest_closed_by_snapshot_policy = last_time is not None and last_time + close_grace <= as_of_utc
    if not metadata_matches:
        issue(0, "PHASE_G_ROW_OR_RANGE_METADATA_MISMATCH")
    if not latest_closed_by_snapshot_policy:
        issue(0, "LAST_BAR_NOT_CLOSED_BY_SNAPSHOT_POLICY")

    hashes_after = _streaming_file_hashes(path)
    stable_during_audit = hashes_before["raw_sha256"] == hashes_after["raw_sha256"]
    if not stable_during_audit:
        issue(0, "SOURCE_CHANGED_DURING_AUDIT")

    structural_ok = (
        header_valid
        and counts["rows"] > 0
        and counts["valid_timestamps"] == counts["rows"]
        and counts["duplicate_timestamps"] == 0
        and counts["out_of_order_timestamps"] == 0
        and counts["pre_2023_rows"] == 0
        and counts["rows_after_as_of"] == 0
        and counts["rows_after_declared_cutoff"] == 0
        and counts["invalid_ohlc_rows"] == 0
        and counts["invalid_volume_rows"] == 0
        and counts["invalid_spread_rows"] == 0
        and hashes_before["lone_cr_count"] == 0
        and metadata_matches
        and latest_closed_by_snapshot_policy
        and stable_during_audit
        and hash_match != "MISMATCH"
    )
    positive_count = counts["positive_spread_rows"]
    zero_count = counts["zero_spread_rows"]
    spread_valid_count = positive_count + zero_count
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "path": f"data/market/{symbol}/{timeframe}.csv",
        "row_count": counts["rows"],
        "unique_timestamp_count": len(timestamps_seen),
        "first_time_utc": actual_first_text,
        "last_time_utc": actual_last_text,
        "declared_effective_cutoff_utc": expected.get("effective_cutoff_utc"),
        "snapshot_as_of_utc": as_of_utc.isoformat(),
        "declared_sha256_semantics": "raw file bytes SHA-256 recorded by phase2_2_2g generator",
        "declared_raw_sha256": declared_raw,
        "actual_raw_sha256": hashes_before["raw_sha256"],
        "actual_canonical_sha256_lf": hashes_before["canonical_sha256"],
        "actual_crlf_variant_sha256": hashes_before["crlf_variant_sha256"],
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "phase_g_hash_comparison": hash_match,
        "file_size_bytes": hashes_before["byte_count"],
        "line_ending_lone_cr_count": hashes_before["lone_cr_count"],
        "snapshot_metadata_matches": metadata_matches,
        "latest_bar_closed_by_snapshot_policy": latest_closed_by_snapshot_policy,
        "source_stable_during_audit": stable_during_audit,
        "counts": counts,
        "ohlc_quality_status": "PASS" if counts["invalid_ohlc_rows"] == 0 else "FAIL",
        "execution_data_quality_status": (
            "SPREAD_FIELDS_VALID_BUT_COSTS_UNVERIFIED" if counts["invalid_spread_rows"] == 0
            else "SPREAD_DATA_INVALID"
        ),
        "spread_quality": {
            "zero_count": zero_count,
            "positive_count": positive_count,
            "missing_count": counts["missing_spread_rows"],
            "invalid_count": counts["invalid_spread_rows"],
            "row_classification_counts": {
                "OBSERVED_VALID": positive_count,
                "OBSERVED_ZERO_UNVERIFIED": zero_count,
                "MISSING": counts["missing_spread_rows"],
                "INVALID": counts["invalid_spread_rows"] - counts["missing_spread_rows"],
                "IMPUTED_ASSUMPTION": 0,
            },
            "zero_percent_of_rows": round(100 * zero_count / counts["rows"], 6) if counts["rows"] else None,
            "positive_percent_of_rows": round(100 * positive_count / counts["rows"], 6) if counts["rows"] else None,
            "positive_percentiles_nearest_rank": {
                f"p{pct}": _spread_percentile(spread_histogram, pct) for pct in (50, 90, 95, 99)
            },
            "positive_max": format(max(spread_histogram), "f") if spread_histogram else None,
            "percentile_histogram_truncated": spread_histogram_truncated,
            "execution_cost_claim": "UNVERIFIED_BROKER_COSTS_NOT_PRESENT_IN_THIS_FILE",
        },
        "cadence": {
            "observed_adjacent_gaps": sum(gap_durations.values()),
            "reported_unconfirmed_intervals_phase_g": expected.get("unconfirmed_intervals"),
            "classification": "UNKNOWN_REQUIRES_OFFICIAL_BROKER_CALENDAR",
            "inferred_missing_bar_count": None,
            "gap_duration_percentiles_nearest_rank": _duration_percentiles(gap_durations),
            "gap_duration_buckets": {
                "under_1_hour": sum(n for sec, n in gap_durations.items() if sec < 3600),
                "1_to_24_hours": sum(n for sec, n in gap_durations.items() if 3600 <= sec < 86400),
                "1_to_3_days": sum(n for sec, n in gap_durations.items() if 86400 <= sec <= 259200),
                "over_3_days": sum(n for sec, n in gap_durations.items() if sec > 259200),
            },
            "potential_shape_counts_not_closure_evidence": gap_shapes,
            "gaps_by_previous_utc_month": dict(sorted(gap_months.items())),
            "gaps_by_previous_utc_weekday": dict(sorted(gap_previous_weekdays.items())),
            "gaps_by_next_utc_weekday": dict(sorted(gap_next_weekdays.items())),
            "all_intervals_remain": "UNKNOWN",
        },
        "issue_samples": issues,
        "status": "PASS" if structural_ok else "FAIL",
    }


def _iso_utc_equal(left: str | None, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        return parse_utc_timestamp(left) == parse_utc_timestamp(right)
    except (ValueError, TypeError, OverflowError):
        return False


def _safe_csv_path(root: Path, symbol: str, timeframe: str) -> Path:
    relative = PurePosixPath("data", "market", symbol, f"{timeframe}.csv")
    if relative.parts[:2] != ("data", "market") or symbol not in SYMBOLS or timeframe not in TIMEFRAMES:
        raise ValueError("PUBLIC_2023_SOURCE_SCOPE_INVALID")
    path = root.joinpath(*relative.parts)
    try:
        checked = ensure_no_symlink_path(root, path)
    except (OSError, ValueError):
        raise ValueError("PUBLIC_2023_SOURCE_PATH_UNSAFE") from None
    if not checked.is_file():
        raise ValueError("PUBLIC_2023_SOURCE_FILE_MISSING")
    return checked


def _load_phase_g_report(root: Path, snapshot_report_path: str = PHASE_G_REPORT) -> tuple[dict[str, Any], str]:
    relative = PurePosixPath(snapshot_report_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("PHASE_G_REPORT_PATH_INVALID")
    path = root.joinpath(*relative.parts)
    try:
        path = ensure_no_symlink_path(root, path)
        raw = path.read_bytes()
        report = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        raise ValueError("PHASE_G_REPORT_UNAVAILABLE_OR_INVALID") from None
    if not isinstance(report, dict):
        raise ValueError("PHASE_G_REPORT_SCHEMA_INVALID")
    return report, hashlib.sha256(raw).hexdigest()


def audit_public_2023_history(root: Path, *, base_sha: str,
                              snapshot_report_path: str = PHASE_G_REPORT) -> dict[str, Any]:
    """Audit the exact public 3×5 CSV inventory against the fixed phase-g cut."""
    root = root.resolve(strict=True)
    errors: list[str] = []
    try:
        snapshot, snapshot_report_sha = _load_phase_g_report(root, snapshot_report_path)
        as_of = parse_utc_timestamp(snapshot.get("as_of_utc", ""))
    except ValueError as exc:
        return {
            "schema_version": 1, "status": "DATA_INTEGRITY_BLOCKED",
            "base_sha": base_sha, "errors": [str(exc)], "series": [],
            "source_lineage_status": "BLOCKED", "as_of_utc": None,
        }
    if snapshot.get("status") != "COMPLETE_VS_MT5_2023_PLUS":
        errors.append("PHASE_G_STATUS_NOT_COMPLETE_VS_MT5_2023_PLUS")
    if _iso_utc_equal(TARGET_START_UTC.isoformat(), snapshot.get("target_start_utc")) is False:
        errors.append("PHASE_G_TARGET_START_MISMATCH")
    expected_items = snapshot.get("items")
    expected_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(expected_items, list):
        for item in expected_items:
            if not isinstance(item, dict):
                errors.append("PHASE_G_SERIES_ENTRY_INVALID")
                continue
            key = (item.get("symbol"), item.get("timeframe"))
            if key in expected_by_key:
                errors.append("PHASE_G_SERIES_DUPLICATE")
            expected_by_key[key] = item
    else:
        errors.append("PHASE_G_SERIES_INVENTORY_MISSING")

    expected_keys = {(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    if set(expected_by_key) != expected_keys:
        errors.append("PHASE_G_SYMBOL_TIMEFRAME_INVENTORY_MISMATCH")

    csv_paths = sorted((root / "data" / "market").glob("*/*.csv"))
    expected_relative_paths = {f"data/market/{symbol}/{timeframe}.csv" for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    actual_relative_paths = {path.relative_to(root).as_posix() for path in csv_paths}
    if actual_relative_paths != expected_relative_paths:
        errors.append("PUBLIC_MARKET_CSV_INVENTORY_MISMATCH")
    if len(csv_paths) != len(expected_keys):
        errors.append("PUBLIC_MARKET_CSV_COUNT_MISMATCH")

    series: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            expected = expected_by_key.get((symbol, timeframe))
            if expected is None:
                errors.append(f"PHASE_G_ENTRY_MISSING:{symbol}:{timeframe}")
                continue
            try:
                path = _safe_csv_path(root, symbol, timeframe)
            except ValueError as exc:
                errors.append(f"{exc}:{symbol}:{timeframe}")
                continue
            report_entry = _audit_csv(path, symbol=symbol, timeframe=timeframe, as_of_utc=as_of,
                                      cutoff_utc=parse_utc_timestamp(expected.get("effective_cutoff_utc", "")),
                                      expected=expected)
            series.append(report_entry)
            if report_entry["status"] != "PASS":
                errors.append(f"SERIES_INTEGRITY_FAILED:{symbol}:{timeframe}")

    total_rows = sum(item["row_count"] for item in series)
    aggregate = snapshot.get("aggregate", {})
    expected_total_rows = aggregate.get("final_2023plus_rows") if isinstance(aggregate, dict) else None
    total_rows_match = total_rows == expected_total_rows == EXPECTED_2023_PLUS_ROWS
    if not total_rows_match:
        errors.append("PUBLIC_2023_PLUS_TOTAL_ROWS_MISMATCH")

    scan_path = root / FROZEN_SCAN_PATH
    try:
        scan_path = ensure_no_symlink_path(root, scan_path)
        scan_hash = _streaming_file_hashes(scan_path)["raw_sha256"] if scan_path.is_file() else None
    except (OSError, ValueError):
        scan_hash = None
    frozen_scan_status = "PASS" if scan_hash == EXPECTED_FROZEN_SCAN_SHA256 else "BLOCKED_HASH_MISMATCH_OR_MISSING"
    if frozen_scan_status != "PASS":
        errors.append("FROZEN_TECHNICAL_SCAN_HASH_MISMATCH")

    no_data_changes = all(item.get("source_stable_during_audit") is True for item in series)
    if not no_data_changes:
        errors.append("SOURCE_CHANGED_DURING_AUDIT")

    result_status = "DATA_INTEGRITY_BLOCKED" if errors else "SOURCE_INTEGRITY_PASS"
    return {
        "schema_version": 1,
        "status": result_status,
        "source_integrity_status": result_status,
        "source_lineage_status": "READY_TO_MANIFEST" if result_status == "SOURCE_INTEGRITY_PASS" else "BLOCKED",
        "base_sha": base_sha,
        "phase_g_report_path": snapshot_report_path,
        "phase_g_report_sha256": snapshot_report_sha,
        "phase_g_status": snapshot.get("status"),
        "target_start_utc": TARGET_START_UTC.isoformat(),
        "as_of_utc": as_of.isoformat(),
        "as_of_semantics": "fixed phase2_2_2g capture time; candle timestamps are open times",
        "source_scope": [f"data/market/{symbol}/{timeframe}.csv" for symbol in SYMBOLS for timeframe in TIMEFRAMES],
        "inventory_count_expected": len(expected_keys),
        "inventory_count_actual": len(csv_paths),
        "aggregate_rows_expected": expected_total_rows,
        "aggregate_rows_actual": total_rows,
        "aggregate_rows_match": total_rows_match,
        "frozen_scan": {
            "path": FROZEN_SCAN_PATH,
            "sha256_expected": EXPECTED_FROZEN_SCAN_SHA256,
            "sha256_actual": scan_hash,
            "status": frozen_scan_status,
        },
        "calendar_status": "UNKNOWN_BROKER_CALENDAR_NOT_VERIFIED",
        "gaps_policy": "Every observed cadence discontinuity remains UNKNOWN; weekend-shaped patterns are candidates only, never broker closures.",
        "discontinuity_count_observed": sum(item["cadence"]["observed_adjacent_gaps"] for item in series),
        "discontinuity_count_reported_phase_g": sum(int(item.get("unconfirmed_intervals") or 0) for item in expected_by_key.values()),
        "continuity_status": "UNKNOWN_NOT_CERTIFIED_BY_REPOSITORY_DATA",
        "series": series,
        "errors": errors,
        "external_calls": 0,
        "llm_calls": 0,
        "mt5_calls": 0,
        "orders_sent": 0,
        "market_data_downloads": 0,
    }


def _source_manifest_payload(audit_report: dict[str, Any]) -> dict[str, Any]:
    if audit_report.get("source_integrity_status") != "SOURCE_INTEGRITY_PASS":
        raise ValueError("PUBLIC_2023_SOURCE_AUDIT_NOT_PASS")
    sources = []
    for item in audit_report.get("series", []):
        sources.append({
            "path": item["path"],
            "symbol": item["symbol"],
            "timeframe": item["timeframe"],
            "source_kind": "PUBLIC_MARKET_2023_PLUS",
            "raw_sha256": item["actual_raw_sha256"],
            "canonical_sha256": item["actual_canonical_sha256_lf"],
            "phase_g_declared_raw_sha256": item["declared_raw_sha256"],
            "phase_g_hash_comparison": item["phase_g_hash_comparison"],
            "file_size_bytes": item["file_size_bytes"],
            "row_count": item["row_count"],
            "first_time_utc": item["first_time_utc"],
            "last_time_utc": item["last_time_utc"],
            "effective_cutoff_utc": item["declared_effective_cutoff_utc"],
            "as_of_utc": audit_report["as_of_utc"],
        })
    sources.sort(key=lambda item: (item["symbol"], item["timeframe"], item["path"]))
    source_fingerprint = _json_sha256(sources)
    return {
        "schema_version": 1,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "hash_semantics": {
            "raw_sha256": "exact current repository file bytes",
            "canonical_sha256": "same CSV bytes after CRLF-to-LF normalization only",
            "phase_g_declared_sha256": "raw bytes emitted by phase2_2_2g; CRLF/LF equivalence is verified by recomputing a line-ending-only variant",
        },
        "dataset_kind": "PUBLIC_2023_PLUS_REPOSITORY_CAPTURE",
        "source_integrity_status": "PASS",
        "run_id": "public-2023plus-asof-20261117T201349Z",
        "base_sha": audit_report["base_sha"],
        "as_of_utc": audit_report["as_of_utc"],
        "phase_g_report_sha256": audit_report["phase_g_report_sha256"],
        "source_manifest_sha256": source_fingerprint,
        "source_count": len(sources),
        "symbol_timeframe_coverage": {symbol: list(TIMEFRAMES) for symbol in SYMBOLS},
        "sources": sources,
    }


def build_public_2023_source_manifest(audit_report: dict[str, Any]) -> dict[str, Any]:
    """Create a generic verified-history source manifest bound to the 15 CSVs."""
    source_manifest = _source_manifest_payload(audit_report)
    validate_history_source_manifest(source_manifest)
    outer = {
        "schema_version": 1,
        "manifest_kind": "PUBLIC_2023_PLUS_SOURCE_LINEAGE",
        "status": "SOURCE_INTEGRITY_PASS",
        "dataset_kind": "PUBLIC_2023_PLUS_REPOSITORY_CAPTURE",
        "evidence_class": "PUBLIC_TRACKED_REPOSITORY_FILES_ONLY",
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "target_start_utc": TARGET_START_UTC.isoformat(),
        "as_of_utc": audit_report["as_of_utc"],
        "base_sha": audit_report["base_sha"],
        "phase_g_report_path": PHASE_G_REPORT,
        "phase_g_report_sha256": audit_report["phase_g_report_sha256"],
        "source_manifest_sha256": source_manifest["source_manifest_sha256"],
        "source_manifest": source_manifest,
    }
    outer["manifest_sha256"] = _json_sha256(outer)
    return outer


def _line_ending_independent(value: dict[str, Any]) -> dict[str, Any]:
    """Drop only fields that necessarily differ when Git rewrites LF/CRLF."""
    return {
        key: item for key, item in value.items()
        if key not in {"raw_sha256", "actual_raw_sha256", "file_size_bytes", "phase_g_hash_comparison"}
    }


def validate_public_2023_source_manifest(root: Path, manifest: Any,
                                         audit_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fail closed on forged, changed, out-of-scope or non-2023+ sources."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("PUBLIC_2023_MANIFEST_SCHEMA_INVALID")
    if manifest.get("status") != "SOURCE_INTEGRITY_PASS":
        raise ValueError("PUBLIC_2023_MANIFEST_NOT_PASS")
    if manifest.get("dataset_kind") != "PUBLIC_2023_PLUS_REPOSITORY_CAPTURE" or manifest.get("evidence_class") != "PUBLIC_TRACKED_REPOSITORY_FILES_ONLY":
        raise ValueError("PUBLIC_2023_MANIFEST_DATA_CLASS_INVALID")
    if (manifest.get("canonicalization_policy") != CANONICAL_HASH_POLICY
            or manifest.get("target_start_utc") != TARGET_START_UTC.isoformat()
            or manifest.get("phase_g_report_path") != PHASE_G_REPORT):
        raise ValueError("PUBLIC_2023_MANIFEST_POLICY_OR_SNAPSHOT_INVALID")
    declared_manifest_hash = manifest.get("manifest_sha256")
    unhashed = dict(manifest)
    unhashed.pop("manifest_sha256", None)
    if declared_manifest_hash != _json_sha256(unhashed):
        raise ValueError("PUBLIC_2023_MANIFEST_FINGERPRINT_MISMATCH")

    source_manifest = manifest.get("source_manifest")
    try:
        validate_history_source_manifest(source_manifest)
    except ValueError as exc:
        raise ValueError(f"PUBLIC_2023_HISTORY_SOURCE_MANIFEST_INVALID:{exc}") from None
    if (source_manifest.get("dataset_kind") != "PUBLIC_2023_PLUS_REPOSITORY_CAPTURE"
            or source_manifest.get("source_integrity_status") != "PASS"
            or source_manifest.get("base_sha") != manifest.get("base_sha")
            or source_manifest.get("as_of_utc") != manifest.get("as_of_utc")
            or source_manifest.get("source_manifest_sha256") != manifest.get("source_manifest_sha256")):
        raise ValueError("PUBLIC_2023_SOURCE_MANIFEST_PROVENANCE_MISMATCH")
    if audit_report is None:
        raise ValueError("PUBLIC_2023_AUDIT_REPORT_REQUIRED_FOR_MANIFEST_VALIDATION")
    if (audit_report.get("source_integrity_status") != "SOURCE_INTEGRITY_PASS"
            or audit_report.get("phase_g_report_sha256") != manifest.get("phase_g_report_sha256")
            or audit_report.get("base_sha") != manifest.get("base_sha")
            or audit_report.get("as_of_utc") != manifest.get("as_of_utc")):
        raise ValueError("PUBLIC_2023_AUDIT_REPORT_PROVENANCE_MISMATCH")

    # The report supplied by the caller is not itself trusted. Re-read the
    # tracked phase-g snapshot and independently re-audit current source bytes
    # before accepting the manifest as real-data provenance.
    _, actual_phase_g_sha = _load_phase_g_report(root, manifest["phase_g_report_path"])
    if actual_phase_g_sha != manifest.get("phase_g_report_sha256"):
        raise ValueError("PUBLIC_2023_PHASE_G_REPORT_HASH_CHANGED")
    fresh_audit = audit_public_2023_history(
        root,
        base_sha=manifest["base_sha"],
        snapshot_report_path=manifest["phase_g_report_path"],
    )
    if (fresh_audit.get("source_integrity_status") != "SOURCE_INTEGRITY_PASS"
            or fresh_audit.get("phase_g_report_sha256") != audit_report.get("phase_g_report_sha256")
            or [_line_ending_independent(item) for item in fresh_audit.get("series", [])]
            != [_line_ending_independent(item) for item in audit_report.get("series", [])]
            or fresh_audit.get("aggregate_rows_actual") != audit_report.get("aggregate_rows_actual")):
        raise ValueError("PUBLIC_2023_FRESH_AUDIT_MISMATCH")

    expected_sources = _source_manifest_payload(fresh_audit)["sources"]
    actual_sources = source_manifest.get("sources")
    if (not isinstance(actual_sources, list) or len(actual_sources) != len(expected_sources)
            or [_line_ending_independent(item) for item in actual_sources]
            != [_line_ending_independent(item) for item in expected_sources]):
        raise ValueError("PUBLIC_2023_MANIFEST_SOURCE_METADATA_MISMATCH")
    if len(actual_sources) != len(SYMBOLS) * len(TIMEFRAMES):
        raise ValueError("PUBLIC_2023_MANIFEST_INVENTORY_COUNT_INVALID")
    seen: set[str] = set()
    root = root.resolve(strict=True)
    for source in actual_sources:
        symbol, timeframe = source.get("symbol"), source.get("timeframe")
        expected_path = f"data/market/{symbol}/{timeframe}.csv"
        relative = source.get("path")
        if (symbol not in SYMBOLS or timeframe not in TIMEFRAMES or relative != expected_path
                or source.get("source_kind") != "PUBLIC_MARKET_2023_PLUS"):
            raise ValueError("PUBLIC_2023_SOURCE_IDENTITY_OR_PATH_INVALID")
        if relative in seen:
            raise ValueError("PUBLIC_2023_SOURCE_DUPLICATE_PATH")
        seen.add(relative)
        path = _safe_csv_path(root, symbol, timeframe)
        actual_hashes = _streaming_file_hashes(path)
        if (actual_hashes["canonical_sha256"] != source.get("canonical_sha256")
                or source.get("raw_sha256") not in {
                    actual_hashes["raw_sha256"],
                    actual_hashes["canonical_sha256"],
                    actual_hashes["crlf_variant_sha256"],
                }):
            raise ValueError("PUBLIC_2023_SOURCE_HASH_CHANGED")
        if _phase_g_hash_match(source.get("phase_g_declared_raw_sha256", ""), actual_hashes) == "MISMATCH":
            raise ValueError("PUBLIC_2023_PHASE_G_CONTENT_HASH_MISMATCH")
    expected_paths = {f"data/market/{symbol}/{timeframe}.csv" for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    actual_paths = {p.relative_to(root).as_posix() for p in (root / "data" / "market").glob("*/*.csv")}
    if actual_paths != expected_paths or seen != expected_paths:
        raise ValueError("PUBLIC_2023_SOURCE_INVENTORY_CHANGED")

    return {
        "status": "PASS",
        "source_count": len(actual_sources),
        "source_manifest_sha256": source_manifest["source_manifest_sha256"],
        "manifest_sha256": declared_manifest_hash,
        "actual_files_rehashed": len(actual_sources),
        "scope": "data/market only; no MT5 snapshots, private chunks, pre-2023 sources, or frozen candidate scan",
    }


def load_public_2023_verified_dataset(root: Path, manifest: dict[str, Any], audit_report: dict[str, Any],
                                      session_manifest: dict[str, Any] | None, **periods: Any):
    """Bridge this source manifest into the existing fail-closed dataset loader."""
    validate_public_2023_source_manifest(root, manifest, audit_report)
    if session_manifest is None:
        raise ValueError("BROKER_CALENDAR_UNKNOWN")
    from src.verified_research_dataset import VerifiedResearchDataset
    return VerifiedResearchDataset.load_from_sources(
        root, manifest["source_manifest"], session_manifest, **periods,
    )
