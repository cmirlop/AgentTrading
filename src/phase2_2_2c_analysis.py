"""Offline broker-calendar, continuity-window and causal-MTF analysis.

This module never contacts MT5 or an LLM provider. It reads the frozen source
CSVs and one previously captured backfill run, verifies their provenance, and
emits aggregate reports without exporting OHLC values.
"""

from __future__ import annotations

import bisect
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from config.backtest_cost_config import COST_SCENARIOS
from config.market_config import RECENT_CANDLES, SYMBOLS, TIMEFRAMES
from config.trade_planner_config import SWING_LOOKBACK_BARS
from src.history_data_integrity import content_hashes, file_content_hashes
from src.historical_trade_planner import SYMBOL_POINT
from src.market_history_audit import (
    REQUIRED_COLUMNS,
    TIMEFRAMES_SECONDS,
    audit_market_history,
    parse_utc_timestamp,
)
from src.market_history_backfill import (
    _plan_fingerprint,
    _plan_snapshot,
)
from src.warmup_audit import ema200_stabilization_bars


RUN_ID = "phase222b-local-20261115"
BASELINE_PATH = Path("data/backtest/technical_candidate_scan.csv")
BASELINE_CANONICAL_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
INDICATOR_MINIMUM_BARS = 200
EMA200_STABLE_BARS = ema200_stabilization_bars()
BROKER_SOURCE_REQUIRED_FIELDS = (
    "official_source",
    "source_url_or_path",
    "sha256",
    "effective_from",
    "effective_to",
    "timezone",
    "session_rule",
)
PRIVATE_KEY_FRAGMENTS = {
    "account_number",
    "account_id",
    "balance",
    "equity",
    "ticket",
    "server",
    "password",
    "api_key",
    "access_token",
    "refresh_token",
    "secret",
}


def utc_iso(value: datetime | int) -> str:
    if isinstance(value, int):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc_epoch(raw: str) -> int:
    value = parse_utc_timestamp(raw)
    return int(value.timestamp())


def _decimal_signature(value: Any, *, price: bool = False) -> str:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("NONFINITE_MARKET_FIELD")
    if price:
        parsed = Decimal(format(float(parsed), ".12g"))
    return format(parsed.normalize(), "f")


def _valid_market_row(row: dict[str, str], timeframe_seconds: int) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    timestamp = parse_utc_epoch(row.get("time", ""))
    if timestamp % timeframe_seconds:
        raise ValueError("TIMEFRAME_GRID_MISALIGNED")
    open_price = Decimal(_decimal_signature(row["open"], price=True))
    high = Decimal(_decimal_signature(row["high"], price=True))
    low = Decimal(_decimal_signature(row["low"], price=True))
    close = Decimal(_decimal_signature(row["close"], price=True))
    if high < low or high < max(open_price, close) or low > min(open_price, close):
        raise ValueError("OHLC_INVALID")
    spread = Decimal(_decimal_signature(row["spread"]))
    tick_volume = Decimal(_decimal_signature(row["tick_volume"]))
    real_volume = Decimal(_decimal_signature(row["real_volume"]))
    if spread < 0 or tick_volume < 0 or real_volume < 0:
        raise ValueError("SPREAD_OR_VOLUME_NEGATIVE")
    ohlc = tuple(_decimal_signature(row[key], price=True) for key in ("open", "high", "low", "close"))
    full = (*ohlc, _decimal_signature(row["tick_volume"]), _decimal_signature(row["spread"]), _decimal_signature(row["real_volume"]))
    return timestamp, ohlc, full


def normalize_local_run_dir(root: Path, run_dir: str | Path | None, default_run_id: str) -> tuple[str, Path]:
    runs_root = (root / "data" / "market_history" / "backfill_runs").resolve()
    if run_dir is None:
        run_id = default_run_id
        run_root = runs_root / run_id
    else:
        candidate = Path(run_dir)
        run_root = candidate if candidate.is_absolute() else root / candidate
        if run_root.is_symlink():
            raise ValueError("LOCAL_RUN_PATH_OUTSIDE_IGNORED_ROOT")
        run_root = run_root.resolve()
        if run_root.parent != runs_root:
            raise ValueError("LOCAL_RUN_PATH_OUTSIDE_IGNORED_ROOT")
        run_id = run_root.name
    if not run_id or run_id in {".", ".."}:
        raise ValueError("LOCAL_RUN_ID_INVALID")
    return run_id, run_root


def verify_run_path(root: Path, run_id: str, run_dir: str | Path | None = None) -> dict[str, Any]:
    """Return presence evidence without creating or repairing any run files."""
    run_id, run_root = normalize_local_run_dir(root, run_dir, run_id)
    manifest = run_root / "manifest.json"
    chunks = run_root / "chunks"
    if not manifest.is_file() or not chunks.is_dir():
        return {
            "status": "MISSING",
            "run_id": run_id,
            "manifest_present": manifest.is_file(),
            "chunks_directory_present": chunks.is_dir(),
            "actual_chunk_count": 0,
        }
    actual = sorted(path.name for path in chunks.iterdir() if path.is_file() and not path.is_symlink())
    return {
        "status": "PRESENT",
        "run_id": run_id,
        "manifest_present": True,
        "chunks_directory_present": True,
        "actual_chunk_count": len(actual),
        "chunk_files": actual,
    }


def source_hashes_match(expected_raw_hashes: dict[str, str], actual_hashes: dict[str, dict[str, str]]) -> bool:
    """Compare the audit's path->raw map with recalculated raw/canonical digests."""
    if set(expected_raw_hashes) != set(actual_hashes):
        return False
    return all(
        actual_hashes[path].get("raw_sha256") == expected_raw_hashes[path]
        and bool(actual_hashes[path].get("canonical_sha256"))
        for path in expected_raw_hashes
    )


def classify_calendar_gap(evidence: dict[str, Any] | None) -> str:
    """Require dated, hashed, official broker evidence; never infer weekdays."""
    if not isinstance(evidence, dict) or any(not evidence.get(key) for key in BROKER_SOURCE_REQUIRED_FIELDS):
        return "INDETERMINATE_BROKER_CALENDAR"
    if evidence.get("official_source") is not True:
        return "INDETERMINATE_BROKER_CALENDAR"
    if evidence.get("session_status") == "CLOSED":
        return "VERIFIED_CLOSED_SESSION"
    if evidence.get("session_status") == "OPEN" and evidence.get("feed_completeness_verified") is True:
        return "VERIFIED_OPEN_SESSION_MISSING"
    if evidence.get("session_status") == "OPEN":
        return "OPEN_SESSION_NO_BAR_FEED_UNVERIFIED"
    return "INDETERMINATE_BROKER_CALENDAR"


def merge_price_variants(variants: Iterable[tuple[str, ...]]) -> tuple[str, tuple[str, ...] | None]:
    """Accept equivalent OHLC with provenance; reject divergent price variants."""
    unique = sorted(set(variants))
    if not unique:
        return "INVALID_DATA", None
    if len(unique) > 1:
        return "SOURCE_CONFLICT", None
    return "OHLC_EQUIVALENT_PROVENANCE_PRESERVED", unique[0]


def build_contiguous_segments(timestamps: list[int], timeframe_seconds: int) -> list[tuple[int, int]]:
    """Return half-open index ranges; a missing cadence slot always splits a segment."""
    if timeframe_seconds <= 0:
        raise ValueError("TIMEFRAME_SECONDS_INVALID")
    ordered = sorted(set(timestamps))
    if not ordered:
        return []
    result: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(ordered)):
        if ordered[index] - ordered[index - 1] != timeframe_seconds:
            result.append((start, index))
            start = index
    result.append((start, len(ordered)))
    return result


def latest_closed_index(timestamps: list[int], timeframe_seconds: int, decision_epoch: int) -> int:
    """Choose only the most recent bar whose close timestamp is <= decision time."""
    return bisect.bisect_right(timestamps, decision_epoch - timeframe_seconds) - 1


def classify_warmup(continuous_bar_count: int) -> str:
    if continuous_bar_count >= EMA200_STABLE_BARS:
        return "EMA200_STABILIZED"
    if continuous_bar_count >= INDICATOR_MINIMUM_BARS:
        return "INDICATORS_READY_EMA200_NOT_STABILIZED"
    return "WARMUP_INSUFFICIENT"


def classify_censored_position(has_complete_future_path: bool, exit_observed: bool) -> str:
    if not has_complete_future_path or not exit_observed:
        return "NOT_EVALUABLE"
    return "EVALUABLE"


def cost_conversion(points: float, point_size: float, lots: float, commission_round_turn_eur_per_lot: float,
                    slippage_points_per_side: float, *, sides: int = 2) -> dict[str, Any]:
    """Convert quoted points exactly to price; keep spread embedded in bid/ask once."""
    values = (points, point_size, lots, commission_round_turn_eur_per_lot, slippage_points_per_side)
    if any(not math.isfinite(float(value)) or float(value) < 0 for value in values) or sides not in {0, 1, 2}:
        raise ValueError("COST_INPUT_INVALID")
    return {
        "spread_points": float(points),
        "point_size": float(point_size),
        "spread_price": float(points) * float(point_size),
        "spread_embedded_in_bid_ask": True,
        "additional_spread_cost": 0.0,
        "spread_double_counted": False,
        "round_turn_commission_eur": float(commission_round_turn_eur_per_lot) * float(lots),
        "slippage_price": float(slippage_points_per_side) * float(point_size) * sides,
        "lot_volume": float(lots),
        "eur_pnl_conversion": "NOT_CALCULATED_WITHOUT_VERIFIED_CONTRACT_AND_CURRENCY_CONVERSION",
    }


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_source_paths(plan: dict[str, Any], run_manifest: dict[str, Any], symbol: str, timeframe: str) -> list[str]:
    paths = [f"data/market/{symbol}/{timeframe}.csv"]
    for snapshot_dir in plan.get("source_snapshot_dirs", []):
        paths.append(f"{snapshot_dir}/{symbol}_{timeframe}.csv")
    planned = run_manifest.get("planned_chunks", {})
    completed = run_manifest.get("chunks", {})
    for chunk_id, chunk in sorted(planned.items()):
        if chunk.get("symbol") == symbol and chunk.get("timeframe") == timeframe:
            entry = completed.get(chunk_id, {})
            if entry.get("file"):
                paths.append(entry["file"])
    return paths


def _source_rank(relative_path: str) -> tuple[int, str]:
    if relative_path.startswith("data/market/"):
        return 0, relative_path
    if relative_path.startswith("data/market_history/mt5_real_readonly/"):
        return 1, relative_path
    if relative_path.startswith("data/market_history/backfill_runs/"):
        return 2, relative_path
    return 3, relative_path


def _load_pair_timeframe(root: Path, paths: list[str], timeframe_seconds: int, as_of_epoch: int,
                         hash_index: dict[str, dict[str, str]],
                         source_file_quality: dict[str, dict[str, Any]]) -> tuple[list[int], dict[str, Any], dict[int, dict[str, Any]]]:
    buckets: dict[int, dict[str, Any]] = {}
    source_rows: dict[str, int] = {}
    source_latest: dict[str, int | None] = {}
    invalid_count = 0
    invalid_samples: list[dict[str, Any]] = []
    open_future_count = 0
    out_of_order_count = 0
    total_rows = 0
    for relative in sorted(paths, key=_source_rank):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError("SELECTED_SOURCE_MISSING_OR_SYMLINK")
        current_rows = 0
        last_ts: int | None = None
        first_valid_ts: int | None = None
        latest_closed_ts: int | None = None
        out_of_order_in_file = 0
        seen_in_file: dict[int, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        duplicate_rows_in_file = 0
        identical_duplicates_in_file = 0
        price_conflicts_in_file = 0
        spread_conflicts_in_file = 0
        invalid_in_file = 0
        zero_spread_in_file = 0
        positive_spread_in_file = 0
        schema_valid = False
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or any(column not in reader.fieldnames for column in REQUIRED_COLUMNS):
                raise ValueError("SELECTED_SOURCE_SCHEMA_INVALID")
            schema_valid = True
            for line_number, row in enumerate(reader, start=2):
                total_rows += 1
                current_rows += 1
                try:
                    timestamp, ohlc, full = _valid_market_row(row, timeframe_seconds)
                except (KeyError, InvalidOperation, ValueError, TypeError):
                    invalid_count += 1
                    invalid_in_file += 1
                    if len(invalid_samples) < 12:
                        invalid_samples.append({"path": relative, "line": line_number, "reason": "ROW_VALIDATION_FAILED"})
                    continue
                first_valid_ts = timestamp if first_valid_ts is None else min(first_valid_ts, timestamp)
                if last_ts is not None and timestamp < last_ts:
                    out_of_order_count += 1
                    out_of_order_in_file += 1
                last_ts = timestamp
                if timestamp + timeframe_seconds > as_of_epoch:
                    open_future_count += 1
                    continue
                latest_closed_ts = timestamp if latest_closed_ts is None else max(latest_closed_ts, timestamp)
                if full[5] == "0":
                    zero_spread_in_file += 1
                elif Decimal(full[5]) > 0:
                    positive_spread_in_file += 1
                if timestamp in seen_in_file:
                    duplicate_rows_in_file += 1
                    prior_ohlc, prior_full = seen_in_file[timestamp]
                    if prior_full == full:
                        identical_duplicates_in_file += 1
                    if prior_ohlc != ohlc:
                        price_conflicts_in_file += 1
                    if prior_full[5] != full[5]:
                        spread_conflicts_in_file += 1
                else:
                    seen_in_file[timestamp] = (ohlc, full)
                bucket = buckets.setdefault(timestamp, {
                    "ohlc": set(),
                    "full": set(),
                    "spreads": set(),
                    "sources": set(),
                    "rows": 0,
                })
                bucket["ohlc"].add(ohlc)
                bucket["full"].add(full)
                bucket["spreads"].add(full[5])
                bucket["sources"].add(relative)
                bucket["rows"] += 1
        source_rows[relative] = current_rows
        source_latest[relative] = last_ts
        source_file_quality[relative] = {
            "path": relative,
            "file_type": "CSV",
            "raw_sha256": hash_index.get(relative, {}).get("raw_sha256"),
            "canonical_sha256": hash_index.get(relative, {}).get("canonical_sha256"),
            "hash_verified": bool(hash_index.get(relative, {}).get("raw_sha256")),
            "schema_valid": schema_valid,
            "required_columns_present": list(REQUIRED_COLUMNS),
            "rows": current_rows,
            "first_bar_open_utc": utc_iso(first_valid_ts) if first_valid_ts is not None else None,
            "last_bar_open_utc": utc_iso(last_ts) if last_ts is not None else None,
            "latest_closed_bar_open_utc": utc_iso(latest_closed_ts) if latest_closed_ts is not None else None,
            "timestamps_are_utc": True,
            "invalid_rows": invalid_in_file,
            "duplicate_timestamp_rows": duplicate_rows_in_file,
            "identical_duplicate_rows": identical_duplicates_in_file,
            "in_file_ohlc_conflicts": price_conflicts_in_file,
            "in_file_spread_conflicts": spread_conflicts_in_file,
            "zero_spread_rows_unverified": zero_spread_in_file,
            "positive_spread_rows_observed": positive_spread_in_file,
            "out_of_order_rows": out_of_order_in_file,
        }

    timestamps = sorted(buckets)
    conflict_times = [timestamp for timestamp, value in buckets.items() if len(value["ohlc"]) > 1]
    valid_times = [timestamp for timestamp in timestamps if len(buckets[timestamp]["ohlc"]) == 1]
    duplicate_rows = sum(max(0, item["rows"] - 1) for item in buckets.values())
    exact_duplicate_rows = sum(max(0, item["rows"] - len(item["full"])) for item in buckets.values())
    spread_variant_times = [timestamp for timestamp, value in buckets.items() if len(value["spreads"]) > 1]
    spread_states = {
        "timestamps_with_any_unverified_zero": sum("0" in value["spreads"] for value in buckets.values()),
        "timestamps_with_positive_spread": sum(any(Decimal(item) > 0 for item in value["spreads"]) for value in buckets.values()),
        "timestamps_with_spread_conflict": len(spread_variant_times),
    }
    metrics = {
        "source_paths": paths,
        "source_row_counts": source_rows,
        "source_latest_open_utc": {key: utc_iso(value) if value is not None else None for key, value in source_latest.items()},
        "source_rows": total_rows,
        "unique_timestamp_count": len(buckets),
        "valid_unique_timestamp_count": len(valid_times),
        "duplicate_timestamp_rows": duplicate_rows,
        "identical_duplicate_rows": exact_duplicate_rows,
        "ohlc_conflict_timestamp_count": len(conflict_times),
        "ohlc_conflict_samples_utc": [utc_iso(value) for value in conflict_times[:50]],
        "invalid_row_count": invalid_count,
        "invalid_row_samples": invalid_samples,
        "open_or_future_bars_excluded": open_future_count,
        "out_of_order_rows": out_of_order_count,
        "spread_quality": spread_states,
        "timeframe_seconds": timeframe_seconds,
    }
    return valid_times, metrics, buckets


def _segment_records(symbol: str, timeframe: str, timestamps: list[int], timeframe_seconds: int,
                     buckets: dict[int, dict[str, Any]], hash_index: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    segments = []
    for start_index, end_index in build_contiguous_segments(timestamps, timeframe_seconds):
        segment_times = timestamps[start_index:end_index]
        source_paths = sorted({path for timestamp in segment_times for path in buckets[timestamp]["sources"]})
        spread_conflicts = sum(len(buckets[timestamp]["spreads"]) > 1 for timestamp in segment_times)
        zero_unverified = sum("0" in buckets[timestamp]["spreads"] for timestamp in segment_times)
        positive = sum(any(Decimal(value) > 0 for value in buckets[timestamp]["spreads"]) for timestamp in segment_times)
        count = len(segment_times)
        start, last = segment_times[0], segment_times[-1]
        end_exclusive = last + timeframe_seconds
        stable = count >= EMA200_STABLE_BARS
        feature_ready = count >= INDICATOR_MINIMUM_BARS
        segment_id = hashlib.sha256(
            f"{symbol}|{timeframe}|{start}|{end_exclusive}|{','.join(source_paths)}".encode("utf-8")
        ).hexdigest()[:20]
        segments.append({
            "window_id": segment_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "interval_utc": {"start_inclusive": utc_iso(start), "end_exclusive": utc_iso(end_exclusive)},
            "first_bar_open_utc": utc_iso(start),
            "last_bar_open_utc": utc_iso(last),
            "last_bar_close_utc": utc_iso(end_exclusive),
            "bar_count": count,
            "contiguous_fixed_cadence": True,
            "indicator_warmup_status": classify_warmup(count),
            "minimum_feature_ready_200": feature_ready,
            "ema200_stabilized_691": stable,
            "window_classification": "TECHNICAL_ONLY_ELIGIBLE" if feature_ready else "NOT_ELIGIBLE",
            "reason": "CONTIGUOUS_AND_EMA200_AVAILABLE" if feature_ready else "LESS_THAN_200_CONTIGUOUS_BARS",
            "source_paths": source_paths,
            "source_sha256": [
                {"path": path, "raw_sha256": hash_index.get(path, {}).get("raw_sha256"),
                 "canonical_sha256": hash_index.get(path, {}).get("canonical_sha256")}
                for path in source_paths
            ],
            "spread_quality": {
                "timestamps_with_any_unverified_zero": zero_unverified,
                "timestamps_with_positive_spread": positive,
                "timestamps_with_spread_conflict": spread_conflicts,
                "execution_eligible": False,
            },
        })
    return segments


def _mtf_decision(symbol: str, decision_epoch: int, series: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per_timeframe: dict[str, Any] = {}
    reasons: list[str] = []
    counts: dict[str, int] = {}
    for timeframe in TIMEFRAMES:
        item = series[timeframe]
        timestamps = item["timestamps"]
        seconds = item["seconds"]
        index = latest_closed_index(timestamps, seconds, decision_epoch)
        if index < 0:
            reasons.append(f"{timeframe}_NO_CLOSED_BAR")
            per_timeframe[timeframe] = {"closed_bar_available": False, "bars_since_gap": 0, "fresh": False}
            continue
        opened = timestamps[index]
        closed = opened + seconds
        age = decision_epoch - closed
        segment_start = item["segment_starts"][index]
        bars_since_gap = index - segment_start + 1
        fresh = 0 <= age <= seconds
        if closed > decision_epoch:
            reasons.append(f"{timeframe}_LOOKAHEAD_REJECTED")
        if not fresh:
            reasons.append(f"{timeframe}_CLOSED_BAR_STALE")
        if bars_since_gap < INDICATOR_MINIMUM_BARS:
            reasons.append(f"{timeframe}_WARMUP_INSUFFICIENT_200")
        counts[timeframe] = bars_since_gap
        per_timeframe[timeframe] = {
            "closed_bar_available": closed <= decision_epoch,
            "latest_closed_bar_open_utc": utc_iso(opened),
            "latest_closed_bar_close_utc": utc_iso(closed),
            "decision_time_utc": utc_iso(decision_epoch),
            "bars_since_last_unresolved_gap": bars_since_gap,
            "fresh_within_one_timeframe": fresh,
            "warmup_status": classify_warmup(bars_since_gap),
        }
    ready = len(counts) == len(TIMEFRAMES) and all(count >= INDICATOR_MINIMUM_BARS for count in counts.values()) and all(
        item["closed_bar_available"] and item["fresh_within_one_timeframe"] for item in per_timeframe.values()
    )
    stable = ready and all(count >= EMA200_STABLE_BARS for count in counts.values())
    return {
        "symbol": symbol,
        "decision_time_utc": utc_iso(decision_epoch),
        "classification": "TECHNICAL_ONLY_ELIGIBLE" if ready else "NOT_ELIGIBLE",
        "feature_ready_all_timeframes": ready,
        "ema200_stabilized_all_timeframes": stable,
        "lookahead_detected": any(reason.endswith("LOOKAHEAD_REJECTED") for reason in reasons),
        "ineligibility_reasons": sorted(set(reasons)),
        "timeframes": per_timeframe,
    }


def _normalized_utc(raw: str | None) -> str | None:
    if not raw:
        return None
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return None
    return utc_iso(value.astimezone(timezone.utc))


def _gap_record(item: dict[str, Any], category: str) -> dict[str, Any]:
    symbol = str(item.get("symbol", "UNKNOWN"))
    timeframe = str(item.get("timeframe", "UNKNOWN"))
    start = item.get("missing_start_utc")
    end = item.get("missing_end_utc")
    interval_id = item.get("interval_id") or hashlib.sha256(
        f"{category}|{symbol}|{timeframe}|{start}|{end}".encode("utf-8")
    ).hexdigest()[:20]
    parts = item.get("classification_ranges", [])
    return {
        "interval_id": interval_id,
        "record_type": category,
        "symbol": symbol,
        "timeframe": timeframe,
        "missing_interval_utc": {
            "start_inclusive": _normalized_utc(start),
            "end_inclusive": _normalized_utc(end),
        },
        "previous_bar_utc": _normalized_utc(item.get("previous_bar_utc")),
        "next_bar_utc": _normalized_utc(item.get("next_bar_utc")),
        "missing_cadence_slots": item.get("missing_bar_count"),
        "classification": "INDETERMINATE_BROKER_CALENDAR",
        "evidence": {
            "broker_calendar_source": None,
            "source_hash": None,
            "rule_applied": "No broker-specific dated session evidence is recorded; no weekday, weekend, DST, or holiday inference is accepted.",
        },
        "component_ranges": [
            {
                "start_utc": _normalized_utc(part.get("start_utc")),
                "end_utc": _normalized_utc(part.get("end_utc")),
                "missing_cadence_slots": part.get("missing_bar_count"),
                "classification": "INDETERMINATE_BROKER_CALENDAR",
            }
            for part in parts
        ],
    }


def _privacy_findings(value: Any, trail: str = "root") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if any(fragment in normalized for fragment in PRIVATE_KEY_FRAGMENTS):
                findings.append(f"{trail}.{key}:SENSITIVE_KEY")
            findings.extend(_privacy_findings(item, f"{trail}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_privacy_findings(item, f"{trail}[{index}]"))
    elif isinstance(value, str):
        if re.search(r"(?i)([a-z]:[\\/]|\\\\[^\\]+\\[^\\]+)", value):
            findings.append(f"{trail}:ABSOLUTE_PATH")
        if re.search(r"(?i)(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]", value):
            findings.append(f"{trail}:CREDENTIAL_PATTERN")
    return findings


def _aggregate_by_symbol_timeframe(items: list[dict[str, Any]], symbols: Iterable[str], timeframes: Iterable[str]) -> dict[str, dict[str, int]]:
    result = {symbol: {timeframe: 0 for timeframe in timeframes} for symbol in symbols}
    for item in items:
        symbol, timeframe = item.get("symbol"), item.get("timeframe")
        if symbol in result and timeframe in result[symbol]:
            result[symbol][timeframe] += 1
    return result


def _aggregate_gap_classifications(
    items: list[dict[str, Any]],
    symbols: Iterable[str],
    timeframes: Iterable[str],
) -> dict[str, dict[str, dict[str, int]]]:
    result = {symbol: {timeframe: {} for timeframe in timeframes} for symbol in symbols}
    for item in items:
        symbol, timeframe = item.get("symbol"), item.get("timeframe")
        if symbol not in result or timeframe not in result[symbol]:
            continue
        classification = item["classification"]
        counts = result[symbol][timeframe]
        counts[classification] = counts.get(classification, 0) + 1
    return result


def generate_phase2_2_2c_reports(
    root: Path,
    *,
    run_tests: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    run_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run focused offline tests, verify source/run integrity, and build reports."""
    root = root.resolve()
    report_dir = root / "reports" / "phase2_2_2c"
    repo = root
    run_id, run_root = normalize_local_run_dir(root, run_dir, RUN_ID)
    progress: list[str] = []

    def notify(stage: str) -> None:
        progress.append(stage)
        if progress_callback:
            progress_callback(stage)

    test_result: dict[str, Any] = {"status": "NOT_RUN", "tests_run": 0, "tests_passed": 0, "tests_failed": 0, "tests_skipped": 0}
    if run_tests:
        import subprocess
        import sys

        patterns = ("test_phase2_2_2*.py", "test_phase1_backtest.py")
        suite_results = []
        for pattern in patterns:
            command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern]
            completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            combined = completed.stdout + "\n" + completed.stderr
            found = re.search(r"Ran\s+(\d+)\s+tests?", combined)
            tests_run = int(found.group(1)) if found else 0
            skipped = len(re.findall(r"\.\.\.\s+skipped\b", combined))
            failures = len(re.findall(r"\b(?:FAIL|ERROR):\s", combined))
            suite_passed = completed.returncode == 0 and found is not None and failures == 0
            suite_results.append({
                "pattern": pattern,
                "status": "PASS" if suite_passed else "FAIL",
                "tests_run": tests_run,
                "tests_passed": max(0, tests_run - skipped - failures) if suite_passed else 0,
                "tests_failed": failures if found else (0 if suite_passed else 1),
                "tests_skipped": skipped,
                "exit_code": completed.returncode,
            })
        tests_run = sum(item["tests_run"] for item in suite_results)
        skipped = sum(item["tests_skipped"] for item in suite_results)
        failures = sum(item["tests_failed"] for item in suite_results)
        passed = sum(item["tests_passed"] for item in suite_results)
        all_tests_pass = all(item["status"] == "PASS" for item in suite_results)
        test_result = {
            "status": "PASS" if all_tests_pass else "FAIL",
            "commands": [
                "python -m unittest discover -s tests -p test_phase2_2_2*.py",
                "python -m unittest discover -s tests -p test_phase1_backtest.py",
            ],
            "suites": suite_results,
            "tests_run": tests_run,
            "tests_passed": passed,
            "tests_failed": failures if found else max(failures, 1),
            "tests_skipped": skipped,
            "exit_code": completed.returncode,
        }
        if test_result["status"] != "PASS":
            # Do not copy full runner output into a public report; it may contain machine paths.
            test_result["failure_summary"] = "See local console output; raw tracebacks are intentionally not published."
        notify("focused_offline_tests_complete")

    initial_report_path = report_dir.parent / "phase2_2_2b" / "initial_gap_audit.json"
    frozen_plan_path = report_dir.parent / "phase2_2_2b" / "frozen_backfill_plan.json"
    if not initial_report_path.is_file() or not frozen_plan_path.is_file():
        raise ValueError("PREVIOUS_PHASE_AUDIT_ARTIFACTS_MISSING")
    initial_report = _read_json(initial_report_path)
    plan = _read_json(frozen_plan_path)
    as_of_raw = initial_report.get("audit_as_of_utc")
    if not as_of_raw:
        raise ValueError("AUDIT_AS_OF_MISSING")
    as_of = datetime.fromisoformat(as_of_raw.replace("Z", "+00:00"))
    if as_of.tzinfo is None:
        raise ValueError("AUDIT_AS_OF_NOT_TIMEZONE_AWARE")
    as_of = as_of.astimezone(timezone.utc)
    as_of_epoch = int(as_of.timestamp())

    manifest_path = run_root / "manifest.json"
    run_presence = verify_run_path(root, run_id, run_root)
    run_manifest: dict[str, Any] = _read_json(manifest_path) if manifest_path.is_file() else {}
    run_matches_plan = False
    if run_manifest:
        manifest_plan = {
            "schema_version": run_manifest.get("schema_version"),
            "canonicalization_policy": run_manifest.get("canonicalization_policy"),
            **run_manifest.get("plan_snapshot", {}),
        }
        run_matches_plan = (
            run_manifest.get("plan_snapshot") == _plan_snapshot(plan)
            and _plan_fingerprint(manifest_plan) == run_manifest.get("plan_sha256")
        )
    notify("local_run_manifest_and_plan_compared")

    baseline_path = root / BASELINE_PATH
    baseline_before = file_content_hashes(baseline_path) if baseline_path.is_file() else None
    source_hashes_before: dict[str, Any] = {}
    source_hashes_after: dict[str, Any] = {}
    audit: dict[str, Any] = {}
    spread_audit: dict[str, Any] = {}
    if run_presence["status"] == "PRESENT":
        audit, spread_audit, _ = audit_market_history(
            root,
            as_of_utc=as_of,
            backfill_run=run_id,
        )
        source_hashes_before = audit.get("source_hashes_before", {})
        source_hashes_after = audit.get("source_hashes_after", {})
        notify("source_audit_complete")
    else:
        audit, spread_audit, _ = audit_market_history(root, as_of_utc=as_of)
        source_hashes_before = audit.get("source_hashes_before", {})
        source_hashes_after = audit.get("source_hashes_after", {})
        notify("source_audit_complete_run_missing")

    hash_index = {
        relative: file_content_hashes(root / relative)
        for relative in sorted(source_hashes_after)
        if (root / relative).is_file()
    }
    window_hash_index = dict(hash_index)
    chunk_hash_inventory = []
    for chunk_id, entry in sorted(run_manifest.get("chunks", {}).items()):
        relative = entry.get("file")
        chunk_path = root / relative if relative else None
        actual = file_content_hashes(chunk_path) if chunk_path and chunk_path.is_file() else {}
        window_hash_index[relative] = actual if relative else {}
        expected_raw = entry.get("raw_sha256")
        expected_canonical = entry.get("canonical_sha256")
        chunk_hash_inventory.append({
            "chunk_id": chunk_id,
            "path": relative,
            "expected_raw_sha256": expected_raw,
            "actual_raw_sha256": actual.get("raw_sha256"),
            "expected_canonical_sha256": expected_canonical,
            "actual_canonical_sha256": actual.get("canonical_sha256"),
            "hashes_match": (
                actual.get("raw_sha256") == expected_raw
                and actual.get("canonical_sha256") == expected_canonical
            ),
        })
    baseline_after = file_content_hashes(baseline_path) if baseline_path.is_file() else None
    source_current_match = source_hashes_match(source_hashes_after, hash_index)
    source_hashes_unchanged = (
        source_hashes_before == source_hashes_after
        and source_current_match
        and source_hashes_match(source_hashes_before, hash_index)
    )
    baseline_ok = bool(
        baseline_before
        and baseline_after
        and baseline_before == baseline_after
        and baseline_after.get("canonical_sha256") == BASELINE_CANONICAL_SHA256
    )

    run_integrity = audit.get("backfill_run", {}) if run_presence["status"] == "PRESENT" else {}
    run_critical_ok = (
        run_presence["status"] == "PRESENT"
        and run_matches_plan
        and run_integrity.get("integrity_status") == "PASS"
        and not run_integrity.get("errors")
        and not run_integrity.get("source_conflicts")
        and audit.get("critical_integrity_errors") == []
        and audit.get("source_files_unchanged") is True
    )
    no_ohlc_conflicts = audit.get("counts", {}).get("price_overlap_conflicts") == 0
    integrity_pass = (
        source_current_match
        and source_hashes_unchanged
        and baseline_ok
        and run_critical_ok
        and no_ohlc_conflicts
        and audit.get("status") in {"PASS", "PASS_WITH_PENDING_GAPS"}
    )
    notify("run_source_and_baseline_integrity_checked")

    # Raw market bars remain in memory only. Public outputs contain timestamps,
    # hashes, counts and statuses; never OHLC, spread values or account fields.
    per_symbol_tf: dict[str, dict[str, Any]] = {}
    series_by_symbol: dict[str, dict[str, Any]] = {}
    source_file_quality: dict[str, dict[str, Any]] = {}
    source_metadata = {
        relative: {
            "path": relative,
            "raw_sha256": hashes.get("raw_sha256"),
            "canonical_sha256": hashes.get("canonical_sha256"),
        }
        for relative, hashes in hash_index.items()
    }
    if run_presence["status"] == "PRESENT" and run_manifest:
        notify("building_gap_safe_contiguous_windows")
        for symbol in SYMBOLS:
            per_symbol_tf[symbol] = {}
            series_by_symbol[symbol] = {}
            for timeframe in TIMEFRAMES:
                seconds = TIMEFRAMES_SECONDS[timeframe]
                paths = _manifest_source_paths(plan, run_manifest, symbol, timeframe)
                timestamps, metrics, buckets = _load_pair_timeframe(
                    root, paths, seconds, as_of_epoch, window_hash_index, source_file_quality
                )
                segment_starts_by_index: list[int] = [0] * len(timestamps)
                segments_index = build_contiguous_segments(timestamps, seconds)
                window_records = _segment_records(symbol, timeframe, timestamps, seconds, buckets, window_hash_index)
                for first, end in segments_index:
                    for index in range(first, end):
                        segment_starts_by_index[index] = first
                series_by_symbol[symbol][timeframe] = {
                    "timestamps": timestamps,
                    "seconds": seconds,
                    "segment_starts": segment_starts_by_index,
                    "segments": window_records,
                    "segments_index": segments_index,
                }
                per_symbol_tf[symbol][timeframe] = {
                    **metrics,
                    "window_count": len(window_records),
                    "feature_ready_window_count": sum(item["minimum_feature_ready_200"] for item in window_records),
                    "ema200_stabilized_window_count": sum(item["ema200_stabilized_691"] for item in window_records),
                    "windows": window_records,
                }
        notify("contiguous_windows_built")

    source_inventory = []
    for relative, hashes in sorted(hash_index.items()):
        quality = source_file_quality.get(relative)
        if quality is None and relative.endswith("/manifest.json"):
            manifest_data = _read_json(root / relative)
            symbols_in_manifest = manifest_data.get("symbols", {})
            quality = {
                "file_type": "SNAPSHOT_MANIFEST",
                "schema_valid": isinstance(symbols_in_manifest, dict)
                and set(symbols_in_manifest) == set(SYMBOLS),
                "snapshot_symbol_count": len(symbols_in_manifest) if isinstance(symbols_in_manifest, dict) else 0,
                "snapshot_timeframe_count": sum(
                    len(value) for value in symbols_in_manifest.values() if isinstance(value, dict)
                ) if isinstance(symbols_in_manifest, dict) else 0,
                "generated_utc": _normalized_utc(manifest_data.get("generated_utc")),
            }
        source_inventory.append({
            "path": relative,
            "raw_sha256": hashes.get("raw_sha256"),
            "canonical_sha256": hashes.get("canonical_sha256"),
            "hash_verified_against_audit": source_current_match,
            **(quality or {"file_type": "UNKNOWN", "schema_valid": False}),
        })
    for chunk in chunk_hash_inventory:
        quality = source_file_quality.get(chunk.get("path"))
        if quality:
            chunk.update({
                "rows": quality["rows"],
                "first_bar_open_utc": quality["first_bar_open_utc"],
                "last_bar_open_utc": quality["last_bar_open_utc"],
                "timestamps_are_utc": quality["timestamps_are_utc"],
                "schema_valid": quality["schema_valid"],
                "invalid_rows": quality["invalid_rows"],
                "duplicate_timestamp_rows": quality["duplicate_timestamp_rows"],
                "in_file_ohlc_conflicts": quality["in_file_ohlc_conflicts"],
                "zero_spread_rows_unverified": quality["zero_spread_rows_unverified"],
                "positive_spread_rows_observed": quality["positive_spread_rows_observed"],
            })

    gap_records: list[dict[str, Any]] = []
    for item in audit.get("pending_recovery_intervals", []):
        gap_records.append(_gap_record(item, "RECOVERY_CANDIDATE"))
    for item in audit.get("calendar_review_intervals", []):
        gap_records.append(_gap_record(item, "CALENDAR_REVIEW"))
    gap_by_classification = {}
    for item in gap_records:
        gap_by_classification[item["classification"]] = gap_by_classification.get(item["classification"], 0) + 1

    mtf_by_symbol: dict[str, Any] = {}
    mtf_eligible_windows: list[dict[str, Any]] = []
    lookahead_count = 0
    for symbol, timeframe_series in series_by_symbol.items():
        decisions = []
        reason_counts: dict[str, int] = {}
        for m15_open in timeframe_series["M15"]["timestamps"]:
            decision_epoch = m15_open + TIMEFRAMES_SECONDS["M15"]
            result = _mtf_decision(symbol, decision_epoch, timeframe_series)
            decisions.append((m15_open, result))
            if result["lookahead_detected"]:
                lookahead_count += 1
            for reason in result["ineligibility_reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1

        feature_ready_count = sum(result["feature_ready_all_timeframes"] for _, result in decisions)
        stable_count = sum(result["ema200_stabilized_all_timeframes"] for _, result in decisions)
        candidate_runs: list[list[tuple[int, dict[str, Any]]]] = []
        current: list[tuple[int, dict[str, Any]]] = []
        for current_item in decisions:
            m15_open, result = current_item
            if not result["feature_ready_all_timeframes"]:
                if current:
                    candidate_runs.append(current)
                    current = []
                continue
            if current and m15_open - current[-1][0] != TIMEFRAMES_SECONDS["M15"]:
                candidate_runs.append(current)
                current = []
            current.append(current_item)
        if current:
            candidate_runs.append(current)
        for group in candidate_runs:
            start = group[0][0]
            end_exclusive = group[-1][0] + TIMEFRAMES_SECONDS["M15"]
            hashes = hashlib.sha256(f"{symbol}|M15_MTF|{start}|{end_exclusive}".encode("utf-8")).hexdigest()[:20]
            mtf_eligible_windows.append({
                "window_id": hashes,
                "symbol": symbol,
                "primary_timeframe": "M15",
                "interval_utc": {"start_inclusive": utc_iso(start), "end_exclusive": utc_iso(end_exclusive)},
                "decision_count": len(group),
                "first_decision_utc": group[0][1]["decision_time_utc"],
                "last_decision_utc": group[-1][1]["decision_time_utc"],
                "classification": "TECHNICAL_ONLY_ELIGIBLE",
                "all_timeframes_had_200_closed_contiguous_bars": True,
                "ema200_stabilized_decisions": sum(item["ema200_stabilized_all_timeframes"] for _, item in group),
                "calendar_verified": False,
                "execution_eligible": False,
            })
        mtf_by_symbol[symbol] = {
            "m15_closed_decisions_examined": len(decisions),
            "technical_feature_ready_all_mtf_decisions": feature_ready_count,
            "ema200_stabilized_all_mtf_decisions": stable_count,
            "feature_ready_window_count": len(candidate_runs),
            "lookahead_rejections": sum(reason_counts.get(f"{timeframe}_LOOKAHEAD_REJECTED", 0) for timeframe in TIMEFRAMES),
            "ineligibility_reason_counts": dict(sorted(reason_counts.items())),
        }

    recovery_count = len(audit.get("pending_recovery_intervals", []))
    calendar_count = len(audit.get("calendar_review_intervals", []))
    broker_calendar = {
        "schema_version": 1,
        "status": "UNAVAILABLE_BROKER_IDENTITY_OR_OFFICIAL_SESSION_SPEC",
        "broker_identity_recorded_in_authorized_run_manifest": any(
            key.lower() in {"broker", "broker_name", "broker_server", "trade_server"}
            for key in run_manifest
        ) if run_manifest else False,
        "official_sources": [],
        "source_policy": {
            "required": "Official broker symbol-session specification or calendar with URL/path, SHA-256, effective dates, timezone and applied rule.",
            "used": [],
            "weekdays_weekends_dst_or_holidays_inferred": False,
        },
        "decision": "No broker-specific source was present in the retained run manifest or committed evidence. No universal FX schedule was assumed; affected intervals remain indeterminate.",
    }

    source_report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if integrity_pass else "FAIL",
        "as_of_utc": as_of.isoformat(),
        "run_id": run_id,
        "run_local_presence": {key: value for key, value in run_presence.items() if key != "chunk_files"},
        "run_integrity": {
            "status": "PASS" if run_critical_ok else "FAIL" if run_presence["status"] == "PRESENT" else "PENDING",
            "manifest_sha256": content_hashes(manifest_path.read_bytes()).get("raw_sha256") if manifest_path.is_file() else None,
            "plan_sha256": run_integrity.get("plan_sha256"),
            "frozen_plan_matches_manifest": run_matches_plan,
            "integrity_status": run_integrity.get("integrity_status"),
            "verified_chunk_count": run_integrity.get("accepted_chunk_count", 0),
            "row_count": run_integrity.get("row_count", 0),
            "price_conflicts": len(run_integrity.get("source_conflicts", [])),
            "chunk_hash_inventory": chunk_hash_inventory,
            "errors": run_integrity.get("errors", []),
            "account_or_terminal_identifiers_included": False,
        },
        "source_hashes_unchanged": source_hashes_unchanged,
        "source_inventory": source_inventory,
        "source_count": len(hash_index),
        "source_rows_audited": audit.get("counts", {}).get("all_source_rows_audited"),
        "baseline_dataset": {
            "path": BASELINE_PATH.as_posix(),
            "raw_sha256_before": baseline_before.get("raw_sha256") if baseline_before else None,
            "raw_sha256_after": baseline_after.get("raw_sha256") if baseline_after else None,
            "canonical_sha256": baseline_after.get("canonical_sha256") if baseline_after else None,
            "expected_canonical_sha256": BASELINE_CANONICAL_SHA256,
            "unchanged": baseline_ok,
        },
        "critical_integrity_errors": audit.get("critical_integrity_errors", []),
        "overlap_price_conflicts": audit.get("counts", {}).get("price_overlap_conflicts"),
        "backfill_source_conflicts": audit.get("counts", {}).get("backfill_source_conflicts"),
        "private_market_bars_published": False,
    }

    windows_report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "as_of_utc": as_of.isoformat(),
        "selection_rule": "Data quality, fixed timeframe cadence, verified source hashes and causal warmup only; no PnL, win rate, signals or favorable outcome enters inclusion.",
        "warmup_policy": {
            "actual_indicator_minimum_bars": INDICATOR_MINIMUM_BARS,
            "basis": "src/indicators.py: EMA200 min_periods=200; RSI14/ATR14 and rolling20 require fewer.",
            "ema200_stability_bars": EMA200_STABLE_BARS,
            "stability_basis": "Existing src/warmup_audit.py 0.1% residual-initialization criterion; reported separately from feature readiness.",
            "required_timeframes_for_existing_intraday_technical_state": list(TIMEFRAMES),
            "recent_candles_config": RECENT_CANDLES,
            "trade_planner_m15_lookback_bars": SWING_LOOKBACK_BARS,
        },
        "source_policy": "All source variants are retained as provenance. OHLC-divergent timestamps are excluded and block their affected windows; identical OHLC may be represented once for technical calculation. Spread variants are never silently selected.",
        "per_symbol_timeframe": per_symbol_tf,
        "multitimeframe_m15_windows": mtf_eligible_windows,
        "execution_eligible_windows": [],
        "reason_execution_windows_empty": "No validated new-bar signal/scan pipeline and no broker-verified historical spread, commission and slippage evidence.",
        "calendar_status": "UNVERIFIED; cadence fragments do not imply market-open or market-closed sessions.",
    }

    gap_report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "broker_calendar_evidence_status": broker_calendar["status"],
        "classification_policy": "All prior heuristic labels such as weekend/weekly-close are treated as unverified unless backed by dated, hashed, official broker session evidence.",
        "counts": {
            "recovery_candidate_records": recovery_count,
            "calendar_review_group_records": calendar_count,
            "records_may_describe_overlapping_cadence_intervals": True,
            "by_final_classification": gap_by_classification,
            "verified_closed_session": 0,
            "verified_open_session_missing": 0,
            "open_session_feed_unverified": 0,
            "indeterminate_broker_calendar": gap_by_classification.get("INDETERMINATE_BROKER_CALENDAR", 0),
            "source_conflict": 0,
            "invalid_data": 0,
        },
        "by_symbol_timeframe_recovery": _aggregate_by_symbol_timeframe(audit.get("pending_recovery_intervals", []), SYMBOLS, TIMEFRAMES),
        "by_symbol_timeframe_calendar_review": _aggregate_by_symbol_timeframe(audit.get("calendar_review_intervals", []), SYMBOLS, TIMEFRAMES),
        "by_symbol_timeframe_classification": _aggregate_gap_classifications(gap_records, SYMBOLS, TIMEFRAMES),
        "intervals": gap_records,
    }

    mtf_report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "decision_time_policy": "Each M15 decision occurs at that bar's close. A timeframe bar is eligible only when its open plus its fixed duration is <= decision_time; otherwise it is excluded.",
        "freshness_policy": "Latest closed bar may be no more than one full timeframe old; older bars are stale until broker session behavior is verified.",
        "warmup_bars": {
            "feature_ready": INDICATOR_MINIMUM_BARS,
            "ema200_stabilized": EMA200_STABLE_BARS,
        },
        "symbols": mtf_by_symbol,
        "decision_count_total": sum(item["m15_closed_decisions_examined"] for item in mtf_by_symbol.values()),
        "feature_ready_total": sum(item["technical_feature_ready_all_mtf_decisions"] for item in mtf_by_symbol.values()),
        "stabilized_total": sum(item["ema200_stabilized_all_mtf_decisions"] for item in mtf_by_symbol.values()),
        "lookahead_rejections": lookahead_count,
        "lookahead_test_status": "PASS" if lookahead_count == 0 else "FAIL",
        "session_calendar_known": False,
        "rolling_or_indicators_across_unresolved_gap": False,
    }

    cost_report = {
        "schema_version": 1,
        "status": "PENDING_NOT_EXECUTED",
        "reason": "No valid signal/scan reconstruction from the recovered bars was established; zero spreads remain unverified and broker commission/slippage are unavailable.",
        "scenario_hypotheses_from_config": COST_SCENARIOS,
        "predeclared_spread_floors_points": [0.0, 5.0, 10.0, 20.0],
        "scenario_or_floor_backtests_executed": 0,
        "no_tuning_or_pnl_based_selection": True,
        "spread_accounting": "Existing historical_execution uses spread-adjusted bid/ask prices; a separate spread charge must not be deducted again.",
        "conversion_validation": {
            symbol: cost_conversion(5.0, SYMBOL_POINT[symbol], 0.1, 7.0, 1.0)
            for symbol in SYMBOLS
        },
        "cost_inputs_verified_from_broker": False,
        "net_profit_or_economic_edge_claimed": False,
    }

    readiness_report = {
        "schema_version": 1,
        "new_bar_backtest_status": "NEW_BAR_BACKTEST_BLOCKED",
        "portfolio_runner_input": {
            "script": "run_portfolio_backtest.py",
            "input_argument": "--scan",
            "default_scan": "data/backtest/technical_candidate_scan.csv",
            "new_bar_scan_reconstruction_exists": False,
        },
        "stateful_runner_input": {
            "script": "run_stateful_backtest.py",
            "input_argument": "--scan",
            "default_scan": "data/backtest/technical_candidate_scan.csv",
            "market_history_loader": "src/historical_state.py:prepare_symbol_history reads data/market only",
            "new_bar_scan_reconstruction_exists": False,
        },
        "frozen_scan": {
            "path": BASELINE_PATH.as_posix(),
            "canonical_sha256": baseline_after.get("canonical_sha256") if baseline_after else None,
            "use_for_new_bar_claim": False,
            "frozen_scan_regression_executed": False,
        },
        "blocking_reasons": [
            "Existing portfolio/stateful runners consume a precomputed --scan; no parity-verified scan producer connects the recovered snapshots/chunks to those runners.",
            "The source dataset is discontinuous under the required fixed-cadence rule and broker calendar evidence is absent.",
            "A full MTF warmup and closed-bar mapping must be verified before any new-bar signal generation.",
            "Historical zero spread is not a verified bid/ask quote; broker commission and slippage are not evidenced.",
        ],
        "confirmatory_economic_evaluation": "BLOCKED",
        "no_backtest_executed_on_recovered_bars": True,
    }

    test_status = test_result.get("status")
    status = (
        "PHASE2_2_2C_BLOCKED_MISSING_LOCAL_RUN" if run_presence["status"] != "PRESENT"
        else "PHASE2_2_2C_BLOCKED_INTEGRITY" if not integrity_pass or test_status == "FAIL"
        else "PHASE2_2_2C_PARTIAL_CALENDAR_UNKNOWN" if gap_by_classification.get("INDETERMINATE_BROKER_CALENDAR", 0) > 0
        else "PHASE2_2_2C_DATA_READY_BACKTEST_BLOCKED"
    )
    summary = {
        "schema_version": 1,
        "status": status,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "head": _git_text(root, "rev-parse", "HEAD"),
            "branch": _git_text(root, "branch", "--show-current"),
            "main_at_run": _git_text(root, "rev-parse", "origin/main"),
            "commit_created": False,
            "push_performed": False,
            "merge_performed": False,
        },
        "environment": {
            "platform": __import__("platform").platform(),
            "python_version": __import__("platform").python_version(),
        },
        "local_run_status": run_presence["status"],
        "source_integrity_status": source_report["status"],
        "offline_tests": test_result,
        "calendar": {
            "verified_closed": 0,
            "verified_open_missing": 0,
            "open_feed_unverified": 0,
            "indeterminate": gap_by_classification.get("INDETERMINATE_BROKER_CALENDAR", 0),
            "recovery_candidates": recovery_count,
            "calendar_review_groups": calendar_count,
        },
        "windows": {
            "m15_decisions_examined": mtf_report["decision_count_total"],
            "technical_feature_ready_mtf_decisions": mtf_report["feature_ready_total"],
            "ema200_stabilized_mtf_decisions": mtf_report["stabilized_total"],
            "technical_only_mtf_windows": len(mtf_eligible_windows),
            "execution_eligible_windows": 0,
            "lookahead_rejections": lookahead_count,
        },
        "backtest": {
            "new_bar_status": readiness_report["new_bar_backtest_status"],
            "executed": False,
            "frozen_scan_regression_executed": False,
            "cost_sensitivity": cost_report["status"],
        },
        "safety": {
            "mt5_connections_in_this_phase": 0,
            "market_data_requests_in_this_phase": 0,
            "llm_provider_calls": 0,
            "orders_sent": 0,
            "positions_modified": 0,
            "private_prices_published": False,
        },
        "critical_checks": {
            "run_manifest_and_all_chunks_verified": run_critical_ok,
            "frozen_plan_matches_local_run": run_matches_plan,
            "47_source_hashes_unchanged": source_hashes_unchanged and len(hash_index) == 47,
            "frozen_dataset_unchanged": baseline_ok,
            "zero_price_conflicts": no_ohlc_conflicts,
            "privacy_scan_pass": True,
            "no_mt5_or_llm_calls": True,
            "lookahead_test_pass": lookahead_count == 0,
            "new_bar_backtest_not_misrepresented": readiness_report["new_bar_backtest_status"] == "NEW_BAR_BACKTEST_BLOCKED",
        },
        "artifacts": [
            "reports/phase2_2_2c/source_integrity.json",
            "reports/phase2_2_2c/broker_calendar_evidence.json",
            "reports/phase2_2_2c/gap_classification.json",
            "reports/phase2_2_2c/eligible_windows.json",
            "reports/phase2_2_2c/mtf_causality_audit.json",
            "reports/phase2_2_2c/dataset_and_backtest_readiness.json",
            "reports/phase2_2_2c/cost_sensitivity.json",
            "reports/phase2_2_2c/next_phase_gates.json",
        ],
    }

    next_gates = {
        "schema_version": 1,
        "state": status,
        "merge_to_main_allowed": False,
        "next_phase_not_started": True,
        "gates": [
            {"id": "BROKER_CALENDAR", "status": "PENDING", "requirement": "Official dated broker symbol-session and holiday evidence with URL/path, SHA-256, validity period, timezone and DST rule."},
            {"id": "LOCAL_RUN_REPRODUCIBILITY", "status": "PASS" if run_critical_ok else "FAIL", "requirement": "Keep the private run manifest and all 98 verified chunks available locally."},
            {"id": "CONTINUOUS_MTF_WARMUP", "status": "PENDING", "requirement": "At least 200 contiguous closed bars per required timeframe for feature readiness; 691 for the existing EMA200 stabilization criterion."},
            {"id": "NEW_BAR_SCAN_PIPELINE", "status": "PENDING", "requirement": "Reconstruct a scan from recovered bars with parity checks against existing deterministic technical components, causal timestamps and no gap crossing."},
            {"id": "EXECUTION_COST_EVIDENCE", "status": "PENDING", "requirement": "Verified historical bid/ask, commission per lot/round turn, slippage assumptions and account-currency conversion."},
            {"id": "EXPLORATORY_BACKTEST", "status": "BLOCKED", "requirement": "Run only after calendar, MTF pipeline, execution eligibility and cost gates pass; preregister BASE/CONSERVATIVE/STRESS and floors."},
        ],
    }

    reports = {
        "source_integrity.json": source_report,
        "broker_calendar_evidence.json": broker_calendar,
        "gap_classification.json": gap_report,
        "eligible_windows.json": windows_report,
        "mtf_causality_audit.json": mtf_report,
        "dataset_and_backtest_readiness.json": readiness_report,
        "cost_sensitivity.json": cost_report,
        "next_phase_gates.json": next_gates,
        "offline_test_results.json": test_result,
        "validation_summary.json": summary,
    }
    privacy_findings: list[str] = []
    for filename, payload in reports.items():
        privacy_findings.extend(f"{filename}:{finding}" for finding in _privacy_findings(payload))
    if privacy_findings:
        summary["status"] = "PHASE2_2_2C_BLOCKED_INTEGRITY"
        summary["critical_checks"]["privacy_scan_pass"] = False
        summary["privacy_findings"] = privacy_findings
        reports["validation_summary.json"] = summary
    for filename, payload in reports.items():
        _json_write(report_dir / filename, payload)
    notify("phase2_2_2c_reports_written")

    return {
        "status": summary["status"],
        "integrity_pass": integrity_pass,
        "run_status": run_presence["status"],
        "source_count": len(hash_index),
        "rows_audited": audit.get("counts", {}).get("all_source_rows_audited"),
        "recovery_candidates": recovery_count,
        "calendar_review_groups": calendar_count,
        "indeterminate_calendar_records": gap_by_classification.get("INDETERMINATE_BROKER_CALENDAR", 0),
        "m15_decisions_examined": mtf_report["decision_count_total"],
        "mtf_feature_ready_decisions": mtf_report["feature_ready_total"],
        "mtf_stabilized_decisions": mtf_report["stabilized_total"],
        "mtf_windows": len(mtf_eligible_windows),
        "offline_tests": test_result,
        "privacy_findings": privacy_findings,
        "report_dir": "reports/phase2_2_2c",
    }


def _git_text(root: Path, *args: str) -> str | None:
    import subprocess

    completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None
