"""Explicit-source, causal technical scan adapter for Phase 2.2.2d.

This module does not alter the frozen scan route. It accepts only a versioned
source manifest with matching hashes and a broker session manifest that passes
the strict calendar gates in ``phase2_2_2d_sessions``.
"""

from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any

import pandas as pd

from config.backtest_config import CURRENT_LIVE_THRESHOLD
from config.market_config import SYMBOLS, TIMEFRAMES
from src.history_data_integrity import CANONICAL_HASH_POLICY, file_content_hashes
from src.market_history_audit import REQUIRED_COLUMNS, parse_utc_timestamp
from src.phase2_2_2d_sessions import (
    TIMEFRAME_SECONDS,
    build_segment_start_index,
    evaluate_broker_session_mtf_decision,
    expected_bar_close_utc,
    validate_session_manifest,
)


SCAN_COLUMNS = (
    "symbol", "analysis_time", "session", "bias", "overall_score", "alignment",
    "setup_quality", "candidate_65", "m5_score", "m15_score", "h1_score", "h4_score", "d1_score",
)


def _safe_relative_path(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or "\\" in raw:
        raise ValueError("SCAN_SOURCE_PATH_INVALID")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("SCAN_SOURCE_PATH_INVALID")
    trusted_root = root.resolve(strict=True)
    candidate = trusted_root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=False).relative_to(trusted_root)
    except ValueError:
        raise ValueError("SCAN_SOURCE_PATH_OUTSIDE_ROOT") from None
    current = trusted_root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise ValueError("SCAN_SOURCE_SYMLINK_REJECTED")
    return candidate


def _csv_identity(relative: str, chunks: dict[str, tuple[str, str]]) -> tuple[str, str, str]:
    path = PurePosixPath(relative)
    parts = path.parts
    if len(parts) == 4 and parts[:2] == ("data", "market"):
        symbol, name = parts[2], path.stem
        return symbol, name, "LEGACY_MARKET_SOURCE"
    normalized = relative
    if normalized in chunks:
        symbol, timeframe = chunks[normalized]
        return symbol, timeframe, "BACKFILL_CHUNK"
    if len(parts) >= 4 and parts[:3] == ("data", "market_history", "mt5_real_readonly"):
        name = path.stem
        if "_" not in name:
            raise ValueError("SCAN_SOURCE_IDENTITY_UNRESOLVED")
        symbol, timeframe = name.rsplit("_", 1)
        return symbol, timeframe, "MT5_READONLY_SNAPSHOT"
    raise ValueError("SCAN_SOURCE_IDENTITY_UNRESOLVED")


def build_history_source_manifest(
    root: Path,
    source_integrity_report: dict[str, Any],
    backfill_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Create a compact, explicit source inventory without exposing bar data."""
    if source_integrity_report.get("status") != "PASS":
        raise ValueError("SCAN_SOURCE_INTEGRITY_NOT_PASS")
    if source_integrity_report.get("source_hashes_unchanged") is not True:
        raise ValueError("SCAN_SOURCE_HASH_AUDIT_NOT_PASS")
    run_id = backfill_manifest.get("run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id):
        raise ValueError("SCAN_RUN_ID_INVALID")
    manifest_chunks = backfill_manifest.get("chunks")
    if not isinstance(manifest_chunks, dict):
        raise ValueError("SCAN_BACKFILL_CHUNKS_INVALID")
    run_prefix = PurePosixPath("data", "market_history", "backfill_runs", run_id, "chunks")
    chunk_index: dict[str, tuple[str, str]] = {}
    for item in manifest_chunks.values():
        if not isinstance(item, dict):
            raise ValueError("SCAN_BACKFILL_CHUNK_INVALID")
        relative = str(item.get("file", "")).replace("\\", "/")
        planned = item.get("planned_chunk", {})
        if not isinstance(planned, dict):
            raise ValueError("SCAN_BACKFILL_PLAN_INVALID")
        if relative and planned.get("symbol") and planned.get("timeframe"):
            if PurePosixPath(relative).parent != run_prefix:
                raise ValueError("SCAN_BACKFILL_CHUNK_RUN_ID_MISMATCH")
            chunk_index[relative] = (planned["symbol"], planned["timeframe"])

    sources = []
    for item in source_integrity_report.get("source_inventory", []):
        relative = item.get("path")
        if not isinstance(relative, str) or not relative.lower().endswith(".csv"):
            continue
        symbol, timeframe, source_kind = _csv_identity(relative, chunk_index)
        if symbol not in SYMBOLS or timeframe not in TIMEFRAMES:
            raise ValueError("SCAN_SOURCE_SCOPE_INVALID")
        path = _safe_relative_path(root, relative)
        actual = file_content_hashes(path)
        if actual.get("raw_sha256") != item.get("raw_sha256") or actual.get("canonical_sha256") != item.get("canonical_sha256"):
            raise ValueError("SCAN_SOURCE_HASH_MISMATCH")
        sources.append({
            "path": relative,
            "symbol": symbol,
            "timeframe": timeframe,
            "source_kind": source_kind,
            "raw_sha256": actual["raw_sha256"],
            "canonical_sha256": actual["canonical_sha256"],
        })
    sources.sort(key=lambda item: (item["symbol"], item["timeframe"], item["path"]))
    covered = {(item["symbol"], item["timeframe"]) for item in sources}
    required = {(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES}
    if not required.issubset(covered):
        raise ValueError("SCAN_REQUIRED_SYMBOL_TIMEFRAME_SOURCE_MISSING")
    fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": 1,
        "canonicalization_policy": CANONICAL_HASH_POLICY,
        "source_integrity_status": "PASS",
        "run_id": run_id,
        "source_manifest_sha256": fingerprint,
        "source_count": len(sources),
        "symbol_timeframe_coverage": {symbol: list(TIMEFRAMES) for symbol in SYMBOLS},
        "sources": sources,
    }


def validate_history_source_manifest(manifest: Any) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("SCAN_SOURCE_MANIFEST_SCHEMA_INVALID")
    if manifest.get("canonicalization_policy") != CANONICAL_HASH_POLICY or manifest.get("source_integrity_status") != "PASS":
        raise ValueError("SCAN_SOURCE_MANIFEST_POLICY_INVALID")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("SCAN_SOURCE_LIST_MISSING")
    expected_fingerprint = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if manifest.get("source_manifest_sha256") != expected_fingerprint:
        raise ValueError("SCAN_SOURCE_MANIFEST_FINGERPRINT_MISMATCH")
    seen_paths: set[str] = set()
    covered: set[tuple[str, str]] = set()
    for item in sources:
        if not isinstance(item, dict):
            raise ValueError("SCAN_SOURCE_ENTRY_INVALID")
        path = item.get("path")
        symbol, timeframe = item.get("symbol"), item.get("timeframe")
        if not isinstance(path, str) or path in seen_paths:
            raise ValueError("SCAN_SOURCE_PATH_DUPLICATE_OR_MISSING")
        if path.startswith("data/backtest/") or path.endswith("technical_candidate_scan.csv"):
            raise ValueError("SCAN_FROZEN_SCAN_SOURCE_FORBIDDEN")
        if symbol not in SYMBOLS or timeframe not in TIMEFRAMES:
            raise ValueError("SCAN_SOURCE_SCOPE_INVALID")
        for field in ("raw_sha256", "canonical_sha256"):
            digest = item.get(field)
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("SCAN_SOURCE_HASH_INVALID")
        seen_paths.add(path)
        covered.add((symbol, timeframe))
    if not {(symbol, timeframe) for symbol in SYMBOLS for timeframe in TIMEFRAMES}.issubset(covered):
        raise ValueError("SCAN_REQUIRED_SYMBOL_TIMEFRAME_SOURCE_MISSING")


def _number(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"SCAN_SOURCE_FIELD_INVALID:{field}") from None
    if not value.is_finite():
        raise ValueError(f"SCAN_SOURCE_FIELD_INVALID:{field}")
    return value


def load_verified_history_frames(
    root: Path,
    source_manifest: dict[str, Any],
    session_manifest: dict[str, Any],
    *,
    evaluation_start_utc: datetime | str,
    evaluation_end_utc: datetime | str,
    as_of_utc: datetime | str,
) -> tuple[dict[str, dict[str, pd.DataFrame]], dict[str, Any]]:
    """Verify source lineage, OHLC uniqueness and closed-bar boundaries."""
    validate_history_source_manifest(source_manifest)
    start = parse_utc_timestamp(str(evaluation_start_utc).replace("+00:00", "Z"))
    end = parse_utc_timestamp(str(evaluation_end_utc).replace("+00:00", "Z"))
    as_of = parse_utc_timestamp(str(as_of_utc).replace("+00:00", "Z"))
    if start >= end or end > as_of:
        raise ValueError("SCAN_EVALUATION_RANGE_INVALID")
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            for instant in (start, end):
                error = validate_session_manifest(session_manifest, symbol, timeframe, instant)
                if error:
                    raise ValueError(f"SCAN_BROKER_CALENDAR_UNVERIFIED:{error}")

    rows: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    provenance: dict[str, list[dict[str, str]]] = {}
    spread_conflicts = 0
    source_rows = 0
    accepted_rows = 0
    for source in source_manifest["sources"]:
        relative = source["path"]
        path = _safe_relative_path(root, relative)
        actual = file_content_hashes(path)
        if actual["raw_sha256"] != source["raw_sha256"] or actual["canonical_sha256"] != source["canonical_sha256"]:
            raise ValueError("SCAN_SOURCE_HASH_MISMATCH")
        symbol, timeframe = source["symbol"], source["timeframe"]
        bucket = rows.setdefault((symbol, timeframe), {})
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(REQUIRED_COLUMNS).issubset(reader.fieldnames):
                raise ValueError("SCAN_SOURCE_COLUMNS_INVALID")
            prior_epoch: int | None = None
            for row in reader:
                source_rows += 1
                try:
                    timestamp = parse_utc_timestamp(row["time"])
                    epoch = int(timestamp.timestamp())
                except (KeyError, ValueError, TypeError, OverflowError):
                    raise ValueError("SCAN_SOURCE_TIMESTAMP_INVALID") from None
                if prior_epoch is not None and epoch < prior_epoch:
                    raise ValueError("SCAN_SOURCE_ORDER_INVALID")
                prior_epoch = epoch
                try:
                    close_time = expected_bar_close_utc(timestamp, timeframe, session_manifest, symbol=symbol)
                except ValueError as exc:
                    raise ValueError(f"SCAN_BAR_BOUNDARY_UNVERIFIED:{exc}") from None
                if close_time > as_of:
                    continue
                values = {field: _number(row[field], field) for field in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")}
                o, h, low, c = (values[key] for key in ("open", "high", "low", "close"))
                if min(o, h, low, c) <= 0 or h < max(o, c) or low > min(o, c) or h < low:
                    raise ValueError("SCAN_SOURCE_OHLC_INVALID")
                if values["tick_volume"] < 0 or values["spread"] < 0 or values["real_volume"] < 0:
                    raise ValueError("SCAN_SOURCE_NONPRICE_FIELD_INVALID")
                key = timestamp.isoformat().replace("+00:00", "Z")
                full_signature = tuple(str(values[field].normalize()) for field in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume"))
                required_signature = tuple(str(values[field].normalize()) for field in ("open", "high", "low", "close", "tick_volume", "real_volume"))
                existing = bucket.get(key)
                source_ref = {"path": relative, "sha256": source["raw_sha256"]}
                if existing is None:
                    bucket[key] = {
                        "time": timestamp,
                        **{field: float(values[field]) for field in values},
                        "bar_close": close_time,
                        "_required_signature": required_signature,
                        "_full_signature": full_signature,
                        "_spread_conflict": False,
                    }
                    provenance[f"{symbol}|{timeframe}|{key}"] = [source_ref]
                else:
                    if existing["_required_signature"] != required_signature:
                        raise ValueError("SCAN_SOURCE_VARIANT_CONFLICT_UNRESOLVED")
                    if existing["_full_signature"] != full_signature:
                        existing["spread"] = math.nan
                        existing["_spread_conflict"] = True
                        spread_conflicts += 1
                    provenance[f"{symbol}|{timeframe}|{key}"].append(source_ref)
                accepted_rows += 1

    frames: dict[str, dict[str, pd.DataFrame]] = {symbol: {} for symbol in SYMBOLS}
    lineage_counts: dict[str, int] = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            key = (symbol, timeframe)
            records = rows.get(key, {})
            if not records:
                raise ValueError("SCAN_SOURCE_DATA_MISSING")
            ordered = [records[item] for item in sorted(records, key=lambda value: parse_utc_timestamp(value))]
            for item in ordered:
                item.pop("_required_signature", None)
                item.pop("_full_signature", None)
                item.pop("_spread_conflict", None)
            frames[symbol][timeframe] = pd.DataFrame(ordered)
            lineage_counts[f"{symbol}|{timeframe}"] = len(records)
    lineage_hash = hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return frames, {
        "source_rows_read": source_rows,
        "closed_rows_accepted": accepted_rows,
        "unique_bar_count_by_symbol_timeframe": lineage_counts,
        "spread_variant_timestamp_count": spread_conflicts,
        "lineage_sha256": lineage_hash,
        "provenance": provenance,
    }


def _technical_candidate(technical: dict[str, Any]) -> bool:
    return (
        technical["setup_quality"] >= CURRENT_LIVE_THRESHOLD
        and technical["alignment"] != "CONFLICT"
        and technical["bias"] in {"BULLISH", "BEARISH"}
    )


def scan_symbol_from_verified_frames(
    symbol: str,
    frames_by_timeframe: dict[str, pd.DataFrame],
    session_manifest: dict[str, Any],
    *,
    evaluation_start_utc: datetime | str,
    evaluation_end_utc: datetime | str,
    source_lineage: dict[str, list[dict[str, str]]] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, int]]:
    """Produce same-contract technical rows only inside verified MTF windows."""
    start = parse_utc_timestamp(str(evaluation_start_utc).replace("+00:00", "Z"))
    end = parse_utc_timestamp(str(evaluation_end_utc).replace("+00:00", "Z"))
    if start >= end:
        raise ValueError("SCAN_EVALUATION_RANGE_INVALID")
    for timeframe in TIMEFRAMES:
        for instant in (start, end):
            error = validate_session_manifest(session_manifest, symbol, timeframe, instant)
            if error:
                raise ValueError(f"SCAN_BROKER_CALENDAR_UNVERIFIED:{error}")

    from src.historical_state import build_historical_market_state, prepare_symbol_history_from_frames
    from src.market_snapshot import build_compact_snapshot
    from src.session_gate import build_session_state
    from src.technical_evaluator import evaluate_technical_state

    prepared = prepare_symbol_history_from_frames(frames_by_timeframe)
    series = {
        timeframe: [int(item.timestamp()) for item in prepared[timeframe]["time"]]
        for timeframe in TIMEFRAMES
    }
    segment_starts = build_segment_start_index(series, symbol, session_manifest)
    m15 = prepared["M15"]
    rows: list[dict[str, Any]] = []
    lineage: list[dict[str, Any]] = []
    counts = {"m15_decisions_examined": 0, "mtf_calendar_or_warmup_blocked": 0, "outside_london_new_york_sessions": 0, "rows_emitted": 0}
    for _, candle in m15.iterrows():
        decision = candle["bar_close"]
        if decision < start or decision > end:
            continue
        counts["m15_decisions_examined"] += 1
        decision_check = evaluate_broker_session_mtf_decision(
            symbol,
            decision.to_pydatetime(),
            series,
            session_manifest,
            segment_starts_by_timeframe=segment_starts,
        )
        if not decision_check["feature_ready_all_timeframes"]:
            counts["mtf_calendar_or_warmup_blocked"] += 1
            continue
        session = build_session_state(decision.to_pydatetime())
        if not session["trading_session_allowed"]:
            counts["outside_london_new_york_sessions"] += 1
            continue
        state = build_historical_market_state(symbol, decision, prepared)
        snapshot = build_compact_snapshot(state, candles_per_timeframe=4)
        technical = evaluate_technical_state(snapshot)
        row = {
            "symbol": symbol,
            "analysis_time": decision,
            "session": "+".join(session["sessions_open"]) if session["sessions_open"] else "NONE",
            "bias": technical["bias"],
            "overall_score": technical["overall_score"],
            "alignment": technical["alignment"],
            "setup_quality": technical["setup_quality"],
            "candidate_65": _technical_candidate(technical),
            "m5_score": technical["timeframe_scores"]["M5"],
            "m15_score": technical["timeframe_scores"]["M15"],
            "h1_score": technical["timeframe_scores"]["H1"],
            "h4_score": technical["timeframe_scores"]["H4"],
            "d1_score": technical["timeframe_scores"]["D1"],
        }
        rows.append(row)
        if source_lineage is not None:
            contributing = {}
            for timeframe, tf_state in state["timeframes"].items():
                bar_time = pd.Timestamp(tf_state["last_bar_open"]).tz_convert("UTC").isoformat().replace("+00:00", "Z")
                contributing[timeframe] = source_lineage.get(f"{symbol}|{timeframe}|{bar_time}", [])
            lineage.append({
                "symbol": symbol,
                "decision_time_utc": decision.isoformat().replace("+00:00", "Z"),
                "calendar_mode": "BROKER_VERIFIED_SESSIONS",
                "mtf_window_id": hashlib.sha256(
                    f"{symbol}|M15_MTF|{decision.isoformat()}".encode()
                ).hexdigest()[:20],
                "timeframe_bars": contributing,
            })
    return pd.DataFrame(rows, columns=SCAN_COLUMNS), lineage, counts


def write_private_scan(
    root: Path,
    frame: pd.DataFrame,
    lineage: list[dict[str, Any]],
    *,
    output_path: str = "reports/private/phase2_2_2d/scan_from_local_history.csv",
) -> tuple[str, str]:
    """Atomically write only a non-empty scan below the ignored private root."""
    if frame.empty:
        raise ValueError("SCAN_NO_VERIFIED_MTF_WINDOWS")
    path = _safe_relative_path(root, output_path)
    private_root = (root.resolve() / "reports" / "private" / "phase2_2_2d").resolve()
    try:
        path.resolve(strict=False).relative_to(private_root)
    except ValueError:
        raise ValueError("SCAN_OUTPUT_MUST_BE_PRIVATE") from None
    if tuple(frame.columns) != SCAN_COLUMNS:
        raise ValueError("SCAN_OUTPUT_CONTRACT_INVALID")
    path.parent.mkdir(parents=True, exist_ok=True)
    lineage_path = path.with_suffix(".lineage.json")
    if path.exists() or lineage_path.exists():
        raise FileExistsError("SCAN_OUTPUT_WOULD_OVERWRITE")
    temporary = path.with_name(f".{path.name}.tmp")
    lineage_temporary = lineage_path.with_name(f".{lineage_path.name}.tmp")
    payload = frame.to_csv(index=False).encode("utf-8")
    lineage_payload = json.dumps(lineage, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
        with lineage_temporary.open("xb") as handle:
            handle.write(lineage_payload)
            handle.flush()
        temporary.replace(path)
        lineage_temporary.replace(lineage_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        lineage_temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(payload).hexdigest(), hashlib.sha256(lineage_payload).hexdigest()
