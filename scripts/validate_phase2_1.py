#!/usr/bin/env python3
"""Offline, reproducible statistical audit for AgentTrading Phase 2.1."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from io import StringIO
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "phase2_1"
BACKTEST_DIR = REPORT_DIR / "backtests"
PRIMARY_DATASET = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
REFERENCE_DATASET_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
EXPECTED_BASELINE = {"BASE": 25.62, "CONSERVATIVE": -27.53, "STRESS": -74.39}
SCENARIOS = ("BASE", "CONSERVATIVE", "STRESS")
THRESHOLDS = (65, 75, 85, 90)
MODES = ("LIMIT", "STOP")
SEED = 20261106

sys.path.insert(0, str(ROOT))
from config.backtest_cost_config import COST_SCENARIOS  # noqa: E402
from src.phase2_1_evaluation import (  # noqa: E402
    assert_not_test_for_tuning,
    attach_event_ids,
    chronological_split,
    month_group_summary,
    purge_trade_events,
    sha256_file,
    summarize_trades,
    utc_timestamp,
    validate_feature_columns,
    weekly_block_bootstrap_mean,
)
from scripts.validate_phase1 import scan_order_execution_calls  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def csv_write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def detect_timestamp_column(frame: pd.DataFrame) -> str | None:
    for column in ("analysis_time", "signal_time", "time", "timestamp", "bar_close", "created_at"):
        if column in frame.columns:
            return column
    return None


def audit_csv(path: Path, root: Path) -> dict:
    frame = pd.read_csv(path, low_memory=False)
    time_column = detect_timestamp_column(frame)
    min_time = max_time = None
    tz_kinds: Counter[str] = Counter()
    invalid_times = 0
    parsed_by_index: dict[int, pd.Timestamp] = {}
    if time_column:
        for index, value in enumerate(frame[time_column].tolist()):
            if pd.isna(value):
                invalid_times += 1
                continue
            try:
                stamp = utc_timestamp(value, field=time_column)
                parsed_by_index[index] = stamp
                raw = str(value).strip()
                tz_kinds["explicit_offset" if re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", raw) else "naive"] += 1
            except ValueError:
                invalid_times += 1
        parsed = list(parsed_by_index.values())
        if parsed:
            min_time = min(parsed).isoformat()
            max_time = max(parsed).isoformat()

    if "symbol" in frame:
        symbols = sorted(frame["symbol"].dropna().astype(str).str.upper().unique().tolist())
        symbol_counts = frame["symbol"].dropna().astype(str).str.upper().value_counts().to_dict()
    else:
        known_symbols = ("EURUSD", "USDCAD", "AUDUSD")
        inferred = [path.parent.name.upper()] if path.parent.name.upper() in known_symbols else [symbol for symbol in known_symbols if symbol in path.name.upper()]
        symbols = inferred
        symbol_counts = {symbol: int(len(frame)) for symbol in inferred}
    duplicate_rows = int(frame.duplicated().sum())
    duplicate_event_keys = None
    if time_column and "symbol" in frame:
        key_times = pd.DataFrame({"symbol": frame["symbol"].astype(str).str.upper(), "time": frame[time_column].astype(str)})
        duplicate_event_keys = int(key_times.duplicated().sum())
        monotonic_by_instrument = all(
            group[time_column].map(lambda value: utc_timestamp(value, field=time_column)).is_monotonic_increasing
            for _, group in frame.groupby("symbol", sort=False)
        )
    elif time_column:
        duplicate_event_keys = int(frame[time_column].duplicated().sum())
        monotonic_by_instrument = all(a <= b for a, b in zip(parsed_by_index.values(), list(parsed_by_index.values())[1:]))
    else:
        monotonic_by_instrument = all(a <= b for a, b in zip(parsed_by_index.values(), list(parsed_by_index.values())[1:]))
    spread_audit = None
    if "spread" in frame.columns:
        spread = pd.to_numeric(frame["spread"], errors="coerce").dropna()
        if len(spread):
            spread_audit = {
                "unit_from_csv": "broker points (as interpreted by the existing simulator; broker-specific semantics unverified)",
                "zero_fraction": round(float((spread == 0).mean()), 8),
                "positive_fraction": round(float((spread > 0).mean()), 8),
                "median_points": float(spread.median()),
                "p95_points": float(spread.quantile(0.95)),
                "p99_points": float(spread.quantile(0.99)),
                "max_points": float(spread.max()),
                "values_at_least_20_points": int((spread >= 20).sum()),
                "quality_warning": "ZERO_DOMINANT_OR_EXTREME_VALUES_UNVERIFIED" if float((spread == 0).mean()) > 0.5 or float(spread.quantile(0.99)) >= 20 else None,
            }
    return {
        "path": path.relative_to(root).as_posix(),
        "format": "CSV",
        "sha256": sha256_file(str(path)),
        "bytes": path.stat().st_size,
        "records": int(len(frame)),
        "instruments": symbols,
        "records_by_instrument": {str(k): int(v) for k, v in symbol_counts.items()},
        "timestamp_column": time_column,
        "timezone_representation": "UTC_explicit_offset" if tz_kinds and not tz_kinds.get("naive") else "mixed_or_naive" if tz_kinds else "not_available",
        "timestamp_value_kinds": dict(tz_kinds),
        "invalid_or_missing_timestamps": invalid_times,
        "min_timestamp_utc": min_time,
        "max_timestamp_utc": max_time,
        "timeframe": path.stem if path.parent.parent.name == "market" and path.stem in {"M5", "M15", "H1", "H4", "D1"} else None,
        "variables": list(frame.columns),
        "missing_cells": int(frame.isna().sum().sum()),
        "missing_cell_fraction": round(float(frame.isna().mean().mean()), 8) if len(frame.columns) else 0.0,
        "duplicate_rows": duplicate_rows,
        "duplicate_symbol_timestamp_keys": duplicate_event_keys,
        "timestamps_monotonic_by_instrument": bool(monotonic_by_instrument),
        "spread_data_audit": spread_audit,
    }


def collect_safe_decision_values(value: object, key: str, output: dict[str, set[str]]) -> None:
    safe_keys = {"provider", "provider_used", "model", "model_name", "prompt_version", "validation_status", "status", "action", "decision", "operation", "thesis", "symbol", "analysis_time", "timestamp", "valid"}
    if isinstance(value, dict):
        for child_key, child in value.items():
            normalized = child_key.lower()
            if normalized in safe_keys and isinstance(child, (str, int, float, bool)):
                output.setdefault(normalized, set()).add(str(child))
            collect_safe_decision_values(child, normalized, output)
    elif isinstance(value, list):
        for child in value:
            collect_safe_decision_values(child, key, output)


def audit_json(path: Path, root: Path) -> dict:
    item = {
        "path": path.relative_to(root).as_posix(),
        "format": "JSONL" if path.suffix.lower() == ".jsonl" else "JSON",
        "sha256": sha256_file(str(path)),
        "bytes": path.stat().st_size,
    }
    try:
        if path.suffix.lower() == ".jsonl":
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            item["records"] = len(records)
            item["top_level_keys"] = sorted({key for record in records if isinstance(record, dict) for key in record})
            item["parse_error"] = None
        else:
            record = json.loads(path.read_text(encoding="utf-8"))
            item["records"] = 1
            item["top_level_keys"] = sorted(record) if isinstance(record, dict) else []
            item["parse_error"] = None
            safe_values: dict[str, set[str]] = {}
            collect_safe_decision_values(record, "", safe_values)
            item["safe_metadata_values"] = {key: sorted(values)[:20] for key, values in safe_values.items()}
    except Exception as error:  # keep the failed source visible in inventory
        item["records"] = None
        item["top_level_keys"] = []
        item["parse_error"] = f"{type(error).__name__}: {error}"
    return item


def source_role(path: Path) -> dict[str, object]:
    relative = path.relative_to(ROOT).as_posix()
    if relative == "data/backtest/technical_candidate_scan.csv":
        return {"role": "primary_technical_opportunities", "eligible_for_features": True, "generation": "candidate scan; exact source command/settings not embedded in the CSV"}
    if relative == "data/backtest/validation_candidate_scan.csv":
        return {"role": "separate_prior_validation_period", "eligible_for_features": True, "generation": "candidate scan; exact source command/settings not embedded in the CSV"}
    if relative.startswith("data/market/"):
        return {"role": "historical_market_bars", "eligible_for_price_execution": True, "generation": "OHLCV export; original tick-by-tick source and retrieval provenance unavailable"}
    if relative.startswith("data/decisions/"):
        return {"role": "saved_agent_decision_log", "eligible_for_deepseek_comparison": False, "generation": "agent message log; model/provider provenance is not stored in a verifiable form"}
    if relative.startswith("data/backtest/level_thesis/"):
        return {"role": "diagnostic_level_outcome_artifact", "eligible_for_features": False, "excluded_reason": "contains post-decision outcomes/future extrema and is not a comparable portfolio trade dataset"}
    if relative.startswith("data/backtest/") and "trade" in path.name:
        return {"role": "historical_trade_output", "eligible_for_reproduction": False, "excluded_reason": "legacy output; current simulator is rerun separately"}
    if relative.startswith("data/backtest/"):
        return {"role": "historical_backtest_artifact", "eligible_for_reproduction": False, "generation": "metadata varies by file; use current simulator outputs for primary comparison"}
    if relative.startswith("data/analysis/") or relative.startswith("data/dry_run/"):
        return {"role": "saved_analysis_or_dry_run", "eligible_for_deepseek_comparison": False, "excluded_reason": "no complete model provenance and not paired with the frozen evaluation outcomes"}
    if relative.startswith("data/state/"):
        return {"role": "mutable_runtime_snapshot", "eligible_for_historical_evaluation": False, "excluded_reason": "current state, not a timestamped historical observation"}
    if relative.startswith("reports/"):
        return {"role": "prior_report", "eligible_as_source_data": False, "excluded_reason": "derived report; not raw event data"}
    return {"role": "other", "eligible_as_source_data": False}


def inventory_sources() -> dict:
    patterns = (
        "data/backtest/**/*.csv", "data/backtest/**/*.json", "data/backtest/**/*.jsonl",
        "data/market/**/*.csv", "data/decisions/**/*.json", "data/analysis/**/*.json",
        "data/dry_run/**/*.json", "data/state/**/*.json",
        "reports/**/*.csv", "reports/**/*.json", "reports/**/*.jsonl",
    )
    paths = sorted({
        path for pattern in patterns for path in ROOT.glob(pattern)
        if path.is_file() and "reports/phase2_1/" not in path.relative_to(ROOT).as_posix()
    })
    entries = []
    for path in paths:
        entry = audit_csv(path, ROOT) if path.suffix.lower() == ".csv" else audit_json(path, ROOT)
        entry.update(source_role(path))
        entries.append(entry)

    candidate = pd.read_csv(PRIMARY_DATASET)
    with_ids = attach_event_ids(candidate, "analysis_time")
    duplicate_ids = int(with_ids["event_id"].duplicated().sum())
    return {
        "generated_at_utc": utc_now(),
        "dataset_policy": "separate sources; no silent concatenation or external downloads",
        "primary_dataset": {
            "path": "data/backtest/technical_candidate_scan.csv",
            "sha256": sha256_file(str(PRIMARY_DATASET)),
            "expected_sha256": REFERENCE_DATASET_SHA256,
            "matches_reference": sha256_file(str(PRIMARY_DATASET)) == REFERENCE_DATASET_SHA256,
            "records": int(len(candidate)),
            "instruments": sorted(candidate["symbol"].astype(str).unique().tolist()),
            "unique_event_ids": int(with_ids["event_id"].nunique()),
            "duplicate_symbol_timestamp_event_ids": duplicate_ids,
        },
        "sources": entries,
        "source_count": len(entries),
        "market_tick_data_available": False,
        "data_generation_provenance_complete": False,
        "notes": [
            "Market CSVs contain UTC-offset OHLCV bars; source broker/server and raw ticks are not identified.",
            "Candidate scan and historical validation scan are different time ranges and remain separate.",
            "Backtest trade CSVs contain labels/outcomes and are not used to build features.",
            "Current data/state snapshots are inventoried but excluded from historical prediction tests.",
        ],
    }


def find_nested_values(value: object, keys: set[str], found: dict[str, set[str]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in keys and isinstance(child, (str, int, float, bool)):
                found.setdefault(key.lower(), set()).add(str(child))
            find_nested_values(child, keys, found)
    elif isinstance(value, list):
        for child in value:
            find_nested_values(child, keys, found)


def audit_llm_decisions(candidate_frame: pd.DataFrame) -> dict:
    paths = sorted((ROOT / "data" / "decisions").rglob("*.json"))
    rows = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found: dict[str, set[str]] = {}
            find_nested_values(data, {"provider", "provider_used", "model", "model_name", "prompt_version"}, found)
            symbol = str(data.get("symbol", "")).upper()
            timestamp = data.get("analysis_time")
            event = None
            if symbol and timestamp:
                from src.phase2_1_evaluation import stable_event_id
                event = stable_event_id({"symbol": symbol, "analysis_time": timestamp})
            rows.append({
                "path": path.relative_to(ROOT).as_posix(),
                "symbol": symbol,
                "timestamp": timestamp,
                "event_id": event,
                "providers": sorted(found.get("provider", set()) | found.get("provider_used", set())),
                "models": sorted(found.get("model", set()) | found.get("model_name", set())),
                "prompt_versions": sorted(found.get("prompt_version", set())),
                "message_count": len(data.get("messages", [])) if isinstance(data.get("messages"), list) else 0,
            })
        except Exception as error:
            rows.append({"path": path.relative_to(ROOT).as_posix(), "parse_error": f"{type(error).__name__}: {error}"})
    candidate_ids = set(attach_event_ids(candidate_frame, "analysis_time")["event_id"])
    matched = [row for row in rows if row.get("event_id") in candidate_ids]
    complete = [row for row in matched if row.get("providers") and row.get("models") and row.get("prompt_versions")]

    cache_path = ROOT / "data/backtest/multiagent_replay/20260408_20260409_USDCAD_events.csv"
    cache_meta = {"records": 0, "source_counts": {}, "action_counts": {}, "overlap_with_primary_scan": 0, "deepseek_attributed": 0}
    if cache_path.exists():
        cache = pd.read_csv(cache_path)
        cache_meta["records"] = int(len(cache))
        for column, target in (("strategy_source", "source_counts"), ("strategy_action", "action_counts")):
            if column in cache:
                cache_meta[target] = {str(k): int(v) for k, v in cache[column].fillna("UNKNOWN").value_counts().to_dict().items()}
        if {"symbol", "analysis_time"}.issubset(cache.columns):
            cache_ids = attach_event_ids(cache, "analysis_time")["event_id"]
            cache_meta["overlap_with_primary_scan"] = int(cache_ids.isin(candidate_ids).sum())
        # The artifact's source label does not identify a DeepSeek model or prompt.
        cache_meta["deepseek_attributed"] = 0

    return {
        "decision_files": len(paths),
        "decision_records_with_matching_primary_event": len(matched),
        "matched_records_with_provider_model_prompt": len(complete),
        "provider_attributed_decisions": sum(bool(row.get("providers")) for row in rows),
        "deepseek_attributed_decisions": sum(any("deepseek" in provider.lower() for provider in row.get("providers", [])) for row in rows),
        "saved_decision_metadata": rows,
        "multiagent_replay_cache": cache_meta,
        "reusable_deepseek_pairs": 0,
        "status": "DEEPSEEK_VALUE_ADD_INCONCLUSIVE_INSUFFICIENT_DATA",
        "reason": "Saved decision logs lack verifiable provider/model/prompt provenance; the archived logs are dated after the primary opportunity scan. The Apr 2026 replay is labeled LLM but does not identify DeepSeek or its prompt.",
        "probe_reports_excluded": ["reports/phase1_llm_live_deepseek.json: provider integration probe, not an event-level strategy decision"],
    }


def audit_candidate_reconstruction() -> dict:
    """Compare an optional current-code reconstruction, then remove only that scratch output."""
    reconstructed_path = ROOT / "data/backtest/phase2_1_reconstructed_candidate_scan.csv"
    reconstructed_summary = ROOT / "data/backtest/phase2_1_reconstructed_candidate_summary.json"
    result: dict[str, object] = {"status": "NOT_RUN", "frozen_dataset_sha256": sha256_file(str(PRIMARY_DATASET))}
    if not reconstructed_path.is_file():
        return result
    frozen = pd.read_csv(PRIMARY_DATASET, low_memory=False)
    regenerated = pd.read_csv(reconstructed_path, low_memory=False)
    key_columns = ["symbol", "analysis_time"]
    frozen = attach_event_ids(frozen, "analysis_time")
    regenerated = attach_event_ids(regenerated, "analysis_time")
    joined = frozen.merge(regenerated, on="event_id", how="outer", suffixes=("_frozen", "_current"), indicator=True, validate="one_to_one")
    differences = {}
    for column in (set(frozen.columns) & set(regenerated.columns)) - {"event_id"}:
        left = joined.get(f"{column}_frozen")
        right = joined.get(f"{column}_current")
        if left is None or right is None:
            continue
        if pd.api.types.is_numeric_dtype(left) or pd.api.types.is_numeric_dtype(right):
            left_num = pd.to_numeric(left, errors="coerce")
            right_num = pd.to_numeric(right, errors="coerce")
            changed = ~((left_num == right_num) | (left_num.isna() & right_num.isna()))
            delta = (left_num.astype(float) - right_num.astype(float)).abs()
            max_abs = float(delta.max()) if delta.notna().any() else None
        else:
            changed = left.fillna("<NA>").astype(str) != right.fillna("<NA>").astype(str)
            max_abs = None
        count = int(changed.sum())
        if count:
            differences[column] = {"changed_rows": count, "max_absolute_difference": max_abs}

    threshold_counts = {}
    for threshold in THRESHOLDS:
        frozen_ok = (frozen["setup_quality"] >= threshold) & (frozen["alignment"] != "CONFLICT") & (frozen["bias"] != "NEUTRAL")
        current_ok = (regenerated["setup_quality"] >= threshold) & (regenerated["alignment"] != "CONFLICT") & (regenerated["bias"] != "NEUTRAL")
        threshold_counts[str(threshold)] = {
            "frozen_count": int(frozen_ok.sum()),
            "current_reconstruction_count": int(current_ok.sum()),
            "event_membership_changes": int((frozen_ok.reset_index(drop=True) != current_ok.reset_index(drop=True)).sum()) if len(frozen_ok) == len(current_ok) and joined["_merge"].eq("both").all() else None,
        }

    source_commit_proc = subprocess.run(["git", "log", "-1", "--format=%H", "--", "data/backtest/technical_candidate_scan.csv"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    source_commit = source_commit_proc.stdout.decode().strip() if source_commit_proc.returncode == 0 else None
    source_scan_proc = subprocess.run(["git", "show", f"{source_commit}:data/backtest/technical_candidate_scan.csv"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False) if source_commit else None
    frozen_at_source_hash = sha256(source_scan_proc.stdout).hexdigest() if source_scan_proc and source_scan_proc.returncode == 0 else None
    logic_comparison = {}
    for relative in ("src/indicators.py", "src/historical_state.py", "src/technical_evaluator.py", "src/market_snapshot.py", "src/session_gate.py"):
        old = subprocess.run(["git", "show", f"{source_commit}:{relative}"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False) if source_commit else None
        current_path = ROOT / relative
        logic_comparison[relative] = {
            "available_at_source_snapshot": bool(old and old.returncode == 0),
            "unchanged_since_snapshot": bool(old and old.returncode == 0 and current_path.is_file() and old.stdout == current_path.read_bytes()),
            "current_sha256": sha256_file(str(current_path)) if current_path.is_file() else None,
        }
    market_changes = []
    if source_commit:
        start = utc_timestamp(frozen["analysis_time"].min(), field="analysis_time")
        end = utc_timestamp(frozen["analysis_time"].max(), field="analysis_time")
        tf_minutes = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}
        for symbol in ("EURUSD", "USDCAD", "AUDUSD"):
            for timeframe, minutes in tf_minutes.items():
                relative = f"data/market/{symbol}/{timeframe}.csv"
                old_proc = subprocess.run(["git", "show", f"{source_commit}:{relative}"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
                current_path = ROOT / relative
                if old_proc.returncode or not current_path.is_file():
                    continue
                old = pd.read_csv(StringIO(old_proc.stdout.decode()), low_memory=False)
                current = pd.read_csv(current_path, low_memory=False)
                old["time"] = pd.to_datetime(old["time"], utc=True)
                current["time"] = pd.to_datetime(current["time"], utc=True)
                old_indexed = old.set_index("time")
                current_indexed = current.set_index("time")
                overlap = old_indexed.index.intersection(current_indexed.index)
                changed = pd.Series(False, index=overlap)
                for column in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume"):
                    if column in old_indexed and column in current_indexed:
                        old_values = pd.to_numeric(old_indexed.loc[overlap, column], errors="coerce").fillna(-999999).round(8)
                        current_values = pd.to_numeric(current_indexed.loc[overlap, column], errors="coerce").fillna(-999999).round(8)
                        changed |= old_values.to_numpy() != current_values.to_numpy()
                before_start_current = current_indexed.index[current_indexed.index < start]
                before_start_old = old_indexed.index[old_indexed.index < start]
                in_window_current = current_indexed.index[(current_indexed.index >= start) & (current_indexed.index <= end)]
                in_window_old = old_indexed.index[(old_indexed.index >= start) & (old_indexed.index <= end)]
                market_changes.append({
                    "symbol": symbol, "timeframe": timeframe,
                    "snapshot_rows": int(len(old)), "current_rows": int(len(current)),
                    "snapshot_min_time": old["time"].min().isoformat(), "current_min_time": current["time"].min().isoformat(),
                    "snapshot_max_time": old["time"].max().isoformat(), "current_max_time": current["time"].max().isoformat(),
                    "overlap_rows": int(len(overlap)), "overlap_bars_with_changed_ohlcv_or_spread": int(changed.sum()),
                    "additional_bars_before_candidate_start": int(len(before_start_current.difference(before_start_old))),
                    "additional_bars_inside_candidate_period": int(len(in_window_current.difference(in_window_old))),
                    "bar_duration_minutes": minutes,
                })

    result.update({
        "status": "RECONSTRUCTION_DIFFERS_AFTER_HISTORICAL_WARMUP_EXTENSION" if differences or not joined["_merge"].eq("both").all() else "EXACT_MATCH",
        "current_reconstruction_sha256": sha256_file(str(reconstructed_path)),
        "source_snapshot_commit": source_commit,
        "dataset_sha256_at_source_snapshot": frozen_at_source_hash,
        "source_snapshot_hash_matches_frozen_dataset": frozen_at_source_hash == result["frozen_dataset_sha256"],
        "technical_logic_source_comparison": logic_comparison,
        "frozen_rows": int(len(frozen)), "reconstructed_rows": int(len(regenerated)),
        "matching_event_ids": int(joined["_merge"].eq("both").sum()),
        "frozen_only_event_ids": int(joined["_merge"].eq("left_only").sum()),
        "reconstructed_only_event_ids": int(joined["_merge"].eq("right_only").sum()),
        "changed_feature_columns": differences,
        "selection_counts_by_existing_threshold": threshold_counts,
        "market_data_snapshot_vs_current": market_changes,
        "interpretation": "The saved scan is the artifact committed with the 2026-10-07 market snapshot. Current market files preserve overlapping historical OHLC/spread rows but include additional pre-period warm-up bars; long-history indicator initialization changes the reconstructed feature values. Keep the known-hash frozen scan for this pre-registered analysis; do not mix the reconstruction.",
        "temporary_reconstruction_removed_after_audit": True,
    })
    reconstructed_path.unlink(missing_ok=True)
    reconstructed_summary.unlink(missing_ok=True)
    return result


def run_offline_tests() -> dict:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_1.py", "-v"]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {
        "name": "phase2_1_unittest_suite",
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "command": command,
        "return_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output": result.stdout[-12000:],
    }


def run_no_lookahead_check() -> dict:
    command = [sys.executable, "test_no_lookahead.py"]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {
        "name": "existing_historical_no_lookahead_check",
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "command": command,
        "return_code": result.returncode,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output": result.stdout[-8000:],
    }


def maybe_run_backtests(run: bool) -> list[dict]:
    outputs = []
    if not run:
        return outputs
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        for engine, script, scan in (
            ("portfolio", "run_portfolio_backtest.py", "technical_candidate_scan.csv"),
            ("stateful", "run_stateful_backtest.py", "data/backtest/technical_candidate_scan.csv"),
        ):
            name = f"{engine}_{scenario}"
            command = [sys.executable, script, "--scan", scan, "--cost-scenario", scenario, "--name", name, "--output-dir", str(BACKTEST_DIR)]
            started = time.monotonic()
            result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            outputs.append({"command": command, "status": "PASS" if result.returncode == 0 else "FAIL", "return_code": result.returncode, "duration_seconds": round(time.monotonic() - started, 3), "output_tail": result.stdout[-5000:]})
            if result.returncode:
                break
    if run and not any(item.get("return_code") for item in outputs):
        # Repeat BASE runs independently to verify reproducibility for both engines.
        for engine, script, scan in (
            ("portfolio", "run_portfolio_backtest.py", "technical_candidate_scan.csv"),
            ("stateful", "run_stateful_backtest.py", "data/backtest/technical_candidate_scan.csv"),
        ):
            name = f"{engine}_BASE_repeat"
            command = [sys.executable, script, "--scan", scan, "--cost-scenario", "BASE", "--name", name, "--output-dir", str(BACKTEST_DIR)]
            started = time.monotonic()
            result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            outputs.append({"command": command, "status": "PASS" if result.returncode == 0 else "FAIL", "return_code": result.returncode, "duration_seconds": round(time.monotonic() - started, 3), "output_tail": result.stdout[-5000:]})
            if result.returncode:
                break
    return outputs


def maybe_reconstruct_scan(run: bool) -> dict:
    if not run:
        return {"status": "NOT_REQUESTED"}
    scan = pd.read_csv(PRIMARY_DATASET)
    start = utc_timestamp(scan["analysis_time"].min(), field="analysis_time").isoformat()
    end = utc_timestamp(scan["analysis_time"].max(), field="analysis_time").isoformat()
    command = [sys.executable, "scan_backtest_candidates.py", "--start", start, "--end", end, "--name", "phase2_1_reconstructed"]
    started = time.monotonic()
    result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return {"status": "PASS" if result.returncode == 0 else "FAIL", "command": command, "return_code": result.returncode, "duration_seconds": round(time.monotonic() - started, 3), "output": result.stdout[-8000:]}


def maybe_run_validation_period(run: bool) -> list[dict]:
    outputs = []
    if not run:
        return outputs
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    for scenario in SCENARIOS:
        for engine, script, scan in (
            ("portfolio", "run_portfolio_backtest.py", "validation_candidate_scan.csv"),
            ("stateful", "run_stateful_backtest.py", "data/backtest/validation_candidate_scan.csv"),
        ):
            name = f"{engine}_validation_{scenario}"
            command = [sys.executable, script, "--scan", scan, "--cost-scenario", scenario, "--name", name, "--output-dir", str(BACKTEST_DIR)]
            started = time.monotonic()
            result = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
            outputs.append({"command": command, "status": "PASS" if result.returncode == 0 else "FAIL", "return_code": result.returncode, "duration_seconds": round(time.monotonic() - started, 3), "output_tail": result.stdout[-5000:]})
            if result.returncode:
                return outputs
    return outputs


def git_provenance() -> dict:
    def git(*args: str) -> str | None:
        result = subprocess.run(["git", *args], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    status = git("status", "--porcelain") or ""
    return {
        "commit": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "working_tree_clean": not bool(status),
        "working_tree_status": status.splitlines(),
        "reference_commit_available": git("cat-file", "-e", "ee10f99b3fa84a07c1fcb06eeb3820d5ef27c5d3^{commit}") is not None,
        "origin_main": git("rev-parse", "origin/main"),
    }


def fingerprint_files() -> dict:
    names = [
        "run_portfolio_backtest.py", "run_stateful_backtest.py", "src/historical_execution.py",
        "src/historical_state.py", "src/historical_trade_planner.py", "src/historical_risk.py",
        "config/backtest_config.py", "config/backtest_cost_config.py", "config/risk_config.py",
        "config/execution_config.py", "data/backtest/technical_candidate_scan.csv",
        "src/phase2_1_evaluation.py", "scripts/validate_phase2_1.py", "tests/test_phase2_1.py",
        "docs/FASE2_1_PLAN.md",
    ]
    return {name: sha256_file(str(ROOT / name)) for name in names}


def load_backtest_outputs() -> tuple[list[dict], dict[str, pd.DataFrame], dict[str, pd.DataFrame], list[str]]:
    comparisons: list[dict] = []
    portfolio: dict[str, pd.DataFrame] = {}
    stateful: dict[str, pd.DataFrame] = {}
    errors = []
    for scenario in SCENARIOS:
        for engine, frame_map in (("portfolio", portfolio), ("stateful", stateful)):
            base = BACKTEST_DIR / f"{engine}_{scenario}"
            summary_path = base.with_name(base.name + "_summary.csv")
            trades_path = base.with_name(base.name + "_trades.csv")
            metadata_path = base.with_name(base.name + "_metadata.json")
            if not (summary_path.is_file() and trades_path.is_file() and metadata_path.is_file()):
                errors.append(f"MISSING_BACKTEST_OUTPUT:{engine}:{scenario}")
                continue
            summary = pd.read_csv(summary_path)
            trades = pd.read_csv(trades_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if str(metadata.get("cost_scenario", "")).upper() != scenario:
                errors.append(f"COST_SCENARIO_MISMATCH:{engine}:{scenario}")
                continue
            if metadata.get("cost_assumptions") != COST_SCENARIOS[scenario]:
                errors.append(f"COST_ASSUMPTIONS_MISMATCH:{engine}:{scenario}")
                continue
            expected_scan = "technical_candidate_scan.csv" if engine == "portfolio" else "data/backtest/technical_candidate_scan.csv"
            if Path(str(metadata.get("source_scan", ""))).name != Path(expected_scan).name:
                errors.append(f"SOURCE_SCAN_MISMATCH:{engine}:{scenario}")
                continue
            frame_map[scenario] = trades
            if engine == "portfolio":
                for _, row in summary.iterrows():
                    comparisons.append(portfolio_summary_row(row, trades, scenario))
            else:
                for _, row in summary.iterrows():
                    comparisons.append(stateful_summary_row(row, scenario, trades))
    return comparisons, portfolio, stateful, errors


def load_validation_period_outputs() -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for scenario in SCENARIOS:
        for engine in ("portfolio", "stateful"):
            name = f"{engine}_validation_{scenario}"
            summary_path = BACKTEST_DIR / f"{name}_summary.csv"
            trades_path = BACKTEST_DIR / f"{name}_trades.csv"
            metadata_path = BACKTEST_DIR / f"{name}_metadata.json"
            if not (summary_path.is_file() and trades_path.is_file() and metadata_path.is_file()):
                errors.append(f"MISSING_VALIDATION_PERIOD_OUTPUT:{engine}:{scenario}")
                continue
            summary = pd.read_csv(summary_path)
            trades = pd.read_csv(trades_path)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if str(metadata.get("cost_scenario", "")).upper() != scenario:
                errors.append(f"VALIDATION_COST_SCENARIO_MISMATCH:{engine}:{scenario}")
                continue
            if metadata.get("cost_assumptions") != COST_SCENARIOS[scenario]:
                errors.append(f"VALIDATION_COST_ASSUMPTIONS_MISMATCH:{engine}:{scenario}")
                continue
            if Path(str(metadata.get("source_scan", ""))).name != "validation_candidate_scan.csv":
                errors.append(f"VALIDATION_SOURCE_SCAN_MISMATCH:{engine}:{scenario}")
                continue
            for _, row in summary.iterrows():
                item = portfolio_summary_row(row, trades, scenario) if engine == "portfolio" else stateful_summary_row(row, scenario, trades)
                item["dataset"] = "validation_candidate_scan"
                rows.append(item)
    return rows, errors


def portfolio_summary_row(summary: pd.Series, trades: pd.DataFrame, scenario: str) -> dict:
    selected = trades[(trades["threshold"] == summary["threshold"]) & (trades["mode"] == summary["mode"])].copy()
    metrics = summarize_trades(selected)
    closed = selected[selected["result"].isin({"WIN", "LOSS"})]
    gross_values = pd.to_numeric(closed.get("gross_pnl_eur", pd.Series(dtype=float)), errors="coerce")
    net_values = pd.to_numeric(closed.get("net_pnl_eur", pd.Series(dtype=float)), errors="coerce")
    crossed_zero = int(((gross_values > 0) & (net_values <= 0) | (gross_values < 0) & (net_values >= 0)).sum()) if len(closed) else 0
    return {
        "engine": "portfolio",
        "strategy": "TECHNICAL_ONLY" if int(summary["threshold"]) == 65 else "TECHNICAL_PLUS_EXISTING_QUALITY_THRESHOLD",
        "scenario": scenario,
        "symbol": "ALL_PORTFOLIO",
        "threshold": int(summary["threshold"]),
        "mode": str(summary["mode"]),
        "opportunities_detected": int(summary.get("opportunities_detected", 0)),
        "proposed": int(summary.get("signals_taken", 0)),
        "accepted_plans": int(summary.get("accepted_plans", summary.get("signals_taken", 0))),
        "rejected_plans": int(summary.get("rejected_plans", summary.get("plan_rejections", 0))),
        "orders_executed": int(summary.get("orders_executed", 0)),
        "resolved": metrics["closed"],
        "ambiguous": metrics["ambiguous"],
        "expired": metrics["expired"],
        "open_or_pending": metrics["open_or_pending"],
        "gross_pnl_eur": metrics["gross_pnl_eur"],
        "costs_eur_closed": metrics["costs_eur_closed"],
        "costs_eur_all": metrics["costs_eur_all"],
        "costs_share_of_gross_pct": round(metrics["costs_eur_closed"] / abs(metrics["gross_pnl_eur"]) * 100, 6) if metrics["gross_pnl_eur"] else None,
        "closed_trades_made_unprofitable_by_added_costs": crossed_zero,
        "net_pnl_eur": metrics["net_pnl_eur"],
        "expectancy_net_eur": metrics["expectancy_net_eur"],
        "expectancy_net_r": metrics["expectancy_net_r"],
        "profit_factor_net": metrics["profit_factor_net"],
        "max_drawdown_net_eur": metrics["max_drawdown_net_eur"],
        "net_win_rate": round(metrics["wins_net"] / metrics["closed"], 8) if metrics["closed"] else None,
        "time_in_market_hours": metrics["time_in_market_hours"],
        "time_in_market_pct_of_filled_span": metrics["time_in_market_pct"],
        "max_simultaneous_filled_positions": metrics["max_simultaneous_filled_positions"],
        "max_open_risk_eur": metrics["max_open_risk_eur"],
    }


def stateful_summary_row(summary: pd.Series, scenario: str, trades: pd.DataFrame | None = None) -> dict:
    selected = pd.DataFrame()
    if trades is not None and not trades.empty:
        selected = trades[(trades["symbol"] == summary["symbol"]) & (trades["threshold"] == summary["threshold"]) & (trades["mode"] == summary["mode"])].copy()
    exposure = summarize_trades(selected) if not selected.empty else {}
    return {
        "engine": "stateful_per_symbol",
        "strategy": "TECHNICAL_ONLY" if int(summary["threshold"]) == 65 else "TECHNICAL_PLUS_EXISTING_QUALITY_THRESHOLD",
        "scenario": scenario,
        "symbol": str(summary["symbol"]),
        "threshold": int(summary["threshold"]),
        "mode": str(summary["mode"]),
        "opportunities_detected": int(summary.get("opportunities_detected", 0)),
        "proposed": int(summary.get("signals_taken", 0)),
        "accepted_plans": int(summary.get("accepted_plans", summary.get("signals_taken", 0))),
        "rejected_plans": int(summary.get("rejected_plans", summary.get("plan_rejections", 0))),
        "orders_executed": int(summary.get("orders_executed", 0)),
        "resolved": int(summary.get("resolved_trades", 0)),
        "ambiguous": int(summary.get("ambiguous_total", int(summary.get("ambiguous_entry_bar", 0)) + int(summary.get("ambiguous_both", 0)))),
        "expired": int(summary.get("expired", 0)),
        "open_or_pending": int(summary.get("open_at_end_total", int(summary.get("open_at_end", 0)) + int(summary.get("pending_at_end", 0)))),
        "gross_pnl_eur": float(summary.get("gross_pnl_eur", 0.0)),
        "costs_eur_closed": float(summary.get("transaction_costs_eur", 0.0)),
        "costs_eur_all": float(summary.get("costs_incurred_eur", 0.0)),
        "net_pnl_eur": float(summary.get("net_pnl_eur", summary.get("gross_pnl_eur", 0.0))),
        "expectancy_net_eur": float(summary.get("net_expectancy_eur")) if pd.notna(summary.get("net_expectancy_eur")) else None,
        "expectancy_net_r": float(summary.get("net_expectancy_r")) if pd.notna(summary.get("net_expectancy_r")) else None,
        "profit_factor_net": float(summary.get("net_profit_factor")) if pd.notna(summary.get("net_profit_factor")) else None,
        "max_drawdown_net_eur": float(summary.get("net_max_drawdown_eur", 0.0)),
        "net_win_rate": float(summary.get("net_win_rate")) if pd.notna(summary.get("net_win_rate")) else None,
        "time_in_market_hours": exposure.get("time_in_market_hours"),
        "time_in_market_pct_of_filled_span": exposure.get("time_in_market_pct"),
        "max_simultaneous_filled_positions": exposure.get("max_simultaneous_filled_positions"),
        "max_open_risk_eur": exposure.get("max_open_risk_eur"),
        "portfolio_constraints": False,
    }


def make_group_reports(primary: pd.DataFrame, portfolio: dict[str, pd.DataFrame], dataset: str = "technical_candidate_scan") -> tuple[list[dict], list[dict], list[dict]]:
    monthly_rows: list[dict] = []
    symbol_rows: list[dict] = []
    regime_rows: list[dict] = []
    features = attach_event_ids(primary, "analysis_time")
    for scenario, all_trades in portfolio.items():
        if all_trades.empty:
            continue
        base = all_trades[(all_trades["threshold"] == 65) & (all_trades["mode"] == "LIMIT")].copy()
        if base.empty:
            continue
        if "signal_time" not in base:
            continue
        base = attach_event_ids(base, "signal_time")
        join_cols = [column for column in ("event_id", "session", "alignment", "bias", "overall_score") if column in features.columns]
        base = base.merge(features[join_cols], on="event_id", how="left", validate="many_to_one", suffixes=("", "_feature"))
        base["scenario"] = scenario
        for item in month_group_summary(base, ["scenario", "threshold", "mode"]):
            item["dataset"] = dataset
            monthly_rows.append(item)
        for symbol, group in base.groupby("symbol", sort=True):
            symbol_rows.append({"dataset": dataset, "scenario": scenario, "symbol": symbol, **summarize_trades(group)})
        base["setup_quality_band"] = pd.cut(
            pd.to_numeric(base["setup_quality"], errors="coerce"),
            bins=[-float("inf"), 49, 64, 74, 84, 89, float("inf")],
            labels=["<50", "50-64", "65-74", "75-84", "85-89", "90+"],
        ).astype("string")
        for field in ("session", "bias", "alignment", "setup_quality_band"):
            if field not in base:
                continue
            for value, group in base.groupby(field, dropna=False, sort=True):
                regime_rows.append({"dataset": dataset, "scenario": scenario, "grouping": field, "group": "UNKNOWN" if pd.isna(value) else str(value), **summarize_trades(group)})
    return monthly_rows, symbol_rows, regime_rows


def split_reports(primary: pd.DataFrame, portfolio: dict[str, pd.DataFrame]) -> tuple[dict, list[dict], list[dict]]:
    assigned, split_info = chronological_split(primary, "analysis_time", embargo_hours=24)
    fold_metrics = []
    split_metrics = []
    for scenario, all_trades in portfolio.items():
        selected = all_trades[(all_trades["threshold"] == 65) & (all_trades["mode"] == "LIMIT")].copy()
        if selected.empty:
            continue
        end_col = "release_time" if "release_time" in selected else "exit_time"
        purged, purge_counts = purge_trade_events(selected, split_info, signal_col="signal_time", end_col=end_col)
        for period in ("TRAIN", "VALIDATION", "TEST"):
            group = purged[purged["period"] == period]
            closed = group[group["result"].isin({"WIN", "LOSS"})]
            ci = weekly_block_bootstrap_mean(closed, seed=SEED)
            split_metrics.append({"dataset": "technical_candidate_scan", "scenario": scenario, "period": period, **summarize_trades(group), "weekly_block_ci95_expectancy": ci.get("ci95"), "independent_week_blocks": ci.get("blocks", 0), "ci_status": ci.get("status"), "purged_reason_counts": purge_counts})

        # Expanding-window descriptive walk-forward: fixed strategy, no tuning.
        dated = assigned[assigned["period"] != "EMBARGO"]
        days = sorted(pd.to_datetime(dated["analysis_time"], utc=True).dt.normalize().unique())
        initial = max(1, int(len(days) * 0.50))
        step = max(1, int(len(days) * 0.10))
        fold = 1
        cursor = initial
        while cursor < len(days) and fold <= 6:
            next_cursor = min(len(days), cursor + step)
            start = pd.Timestamp(days[cursor])
            start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
            if next_cursor < len(days):
                stop = pd.Timestamp(days[next_cursor])
                stop = stop.tz_localize("UTC") if stop.tzinfo is None else stop.tz_convert("UTC")
            else:
                stop = pd.Timestamp(days[-1])
                stop = stop.tz_localize("UTC") if stop.tzinfo is None else stop.tz_convert("UTC")
                stop += pd.Timedelta(days=1)
            signal_values = pd.to_datetime(selected["signal_time"], utc=True)
            embargo_delta = pd.Timedelta(hours=int(split_info["embargo_hours"]))
            mask = (signal_values >= start + embargo_delta) & (signal_values < stop - embargo_delta)
            fold_group = selected.loc[mask].copy()
            # Purge the evaluation window's right-edge crossing labels.
            fold_ends = pd.to_datetime(fold_group[end_col], utc=True, errors="coerce")
            fold_group = fold_group.loc[fold_ends.notna() & (fold_ends >= signal_values.loc[fold_group.index]) & (fold_ends < stop - embargo_delta)]
            fold_metrics.append({"scenario": scenario, "fold": fold, "test_start_utc": (start + embargo_delta).isoformat(), "test_end_exclusive_utc": (stop - embargo_delta).isoformat(), **summarize_trades(fold_group), "status": "DESCRIPTIVE_FIXED_STRATEGY"})
            cursor = next_cursor
            fold += 1
    split_info["holdout_is_confirmatory"] = False
    split_info["holdout_limitation"] = "The primary interval is explicitly marked EXPLORATION_DAYS in the repository config and was previously examined; TEST is a chronological holdout in this report but not an untouched confirmatory sample."
    split_info["test_protected_from_tuning"] = True
    split_info["walk_forward_folds"] = len(fold_metrics) // max(1, len(portfolio))
    return split_info, split_metrics, fold_metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-backtests", action="store_true", help="Re-run all six primary-scan backtests plus BASE repeat outputs")
    parser.add_argument("--run-validation-period", action="store_true", help="Also run BASE/CONSERVATIVE/STRESS on validation_candidate_scan.csv")
    parser.add_argument("--reconstruct-current-candidates", action="store_true", help="Recreate the primary scan from current local bars and compare it with the frozen scan")
    parser.add_argument("--skip-offline-tests", action="store_true", help="Skip the Phase 2.1 unittest suite; reports are marked PENDING")
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    backtest_commands = maybe_run_backtests(args.run_backtests)
    validation_commands = maybe_run_validation_period(args.run_validation_period)
    reconstruction_run = maybe_reconstruct_scan(args.reconstruct_current_candidates)
    reconstruction = audit_candidate_reconstruction()
    previous_reconstruction_path = REPORT_DIR / "reconstruction_diff.json"
    if reconstruction.get("status") == "NOT_RUN" and previous_reconstruction_path.is_file():
        previous = json.loads(previous_reconstruction_path.read_text(encoding="utf-8"))
        if previous.get("frozen_dataset_sha256") == sha256_file(str(PRIMARY_DATASET)) and previous.get("status") not in {None, "NOT_RUN"}:
            reconstruction = previous
            reconstruction["previous_verified_reconstruction_reused"] = True
    reconstruction["reconstruction_invocation"] = reconstruction_run
    if reconstruction.get("status") == "NOT_RUN":
        reconstruction["reproducible_command"] = "python scripts/validate_phase2_1.py --reconstruct-current-candidates"
    json_write(REPORT_DIR / "reconstruction_diff.json", reconstruction)
    inventory = inventory_sources()
    json_write(REPORT_DIR / "data_inventory.json", inventory)

    primary = pd.read_csv(PRIMARY_DATASET)
    llm_audit = audit_llm_decisions(primary)
    json_write(REPORT_DIR / "llm_value_add.json", llm_audit)

    baseline_rows, portfolio, stateful, output_errors = load_backtest_outputs()
    validation_rows, validation_output_errors = load_validation_period_outputs()
    if validation_rows:
        csv_write(REPORT_DIR / "validation_period_comparison.csv", validation_rows)
    if baseline_rows:
        csv_write(REPORT_DIR / "baseline_comparison.csv", baseline_rows)
    primary_portfolio = [row for row in baseline_rows if row["engine"] == "portfolio"]
    strategy_rows = [{
        "engine": "none",
        "strategy": "ALWAYS_HOLD",
        "scenario": scenario,
        "symbol": "ALL_PORTFOLIO",
        "threshold": None,
        "mode": "NONE",
        "opportunities_detected": len(primary),
        "proposed": 0,
        "accepted_plans": 0,
        "rejected_plans": 0,
        "orders_executed": 0,
        "resolved": 0,
        "ambiguous": 0,
        "expired": 0,
        "open_or_pending": 0,
        "gross_pnl_eur": 0.0,
        "costs_eur_closed": 0.0,
        "costs_eur_all": 0.0,
        "net_pnl_eur": 0.0,
        "expectancy_net_eur": None,
        "expectancy_net_r": None,
        "profit_factor_net": None,
        "max_drawdown_net_eur": 0.0,
        "net_win_rate": None,
    } for scenario in SCENARIOS]
    strategy_rows.extend(primary_portfolio)
    strategy_rows.append({"engine": "historical_decisions", "strategy": "TECHNICAL_PLUS_DEEPSEEK", "scenario": "NOT_EVALUABLE", "status": "INCONCLUSIVE_INSUFFICIENT_DATA", "matched_pairs": 0, "reason": llm_audit["reason"]})
    csv_write(REPORT_DIR / "strategy_comparison.csv", strategy_rows)
    json_write(REPORT_DIR / "strategy_comparison.json", {
        "primary_strategy": "TECHNICAL_ONLY threshold=65 LIMIT with portfolio constraints",
        "quality_thresholds_descriptive_only": list(THRESHOLDS),
        "always_hold": "zero trades, zero PnL; abstention comparator only",
        "technical_variants": primary_portfolio,
        "separate_prior_validation_period": validation_rows,
        "deepseek": {"status": llm_audit["status"], "matched_pairs": 0, "reason": llm_audit["reason"]},
        "ollama": {"status": "NOT_EVALUATED", "reason": "No provenance-equivalent historical decision set found."},
        "comparability_note": "Threshold variants can have different slot occupancy and portfolio schedules; differences are descriptive, not an isolated causal effect of filtering.",
    })

    monthly, symbols, regimes = make_group_reports(primary, portfolio)
    # Keep the older scan as a separately labelled dataset while providing
    # the same descriptive month/symbol/regime cuts. It must never be merged
    # with the primary period or used to select a configuration.
    validation_scan_path = ROOT / "data/backtest/validation_candidate_scan.csv"
    if validation_rows and validation_scan_path.is_file():
        validation_scan = pd.read_csv(validation_scan_path)
        validation_portfolio = {
            scenario: pd.read_csv(BACKTEST_DIR / f"portfolio_validation_{scenario}_trades.csv")
            for scenario in SCENARIOS
            if (BACKTEST_DIR / f"portfolio_validation_{scenario}_trades.csv").is_file()
        }
        v_monthly, v_symbols, v_regimes = make_group_reports(
            validation_scan, validation_portfolio, dataset="validation_candidate_scan"
        )
        monthly.extend(v_monthly)
        symbols.extend(v_symbols)
        regimes.extend(v_regimes)
    csv_write(REPORT_DIR / "period_analysis.csv", monthly)
    csv_write(REPORT_DIR / "symbol_analysis.csv", symbols)
    csv_write(REPORT_DIR / "regime_analysis.csv", regimes)
    split_info, split_metrics, walk_forward = split_reports(primary, portfolio)
    json_write(REPORT_DIR / "time_split.json", {"split": split_info, "metrics": split_metrics, "walk_forward": walk_forward})
    csv_write(REPORT_DIR / "walk_forward.csv", walk_forward)

    # Statistical report uses the unchanged current portfolio strategy and all cost cases.
    statistical = {"seed": SEED, "method": "weekly block bootstrap, UTC exit weeks", "primary_strategy": "portfolio threshold 65 LIMIT", "scenarios": {}}
    for scenario, trades in portfolio.items():
        selected = trades[(trades["threshold"] == 65) & (trades["mode"] == "LIMIT")].copy()
        split_closed = []
        test_info = next((row for row in split_metrics if row["scenario"] == scenario and row["period"] == "TEST"), None)
        if test_info:
            end_col = "release_time" if "release_time" in selected else "exit_time"
            purged, _ = purge_trade_events(selected, split_info, signal_col="signal_time", end_col=end_col)
            split_closed = purged[(purged["period"] == "TEST") & purged["result"].isin({"WIN", "LOSS"})]
        statistical["scenarios"][scenario] = {
            "all_period_weekly_block_ci95": weekly_block_bootstrap_mean(selected, seed=SEED),
            "test_weekly_block_ci95": weekly_block_bootstrap_mean(pd.DataFrame(split_closed), seed=SEED),
            "test_metrics": test_info,
        }
    json_write(REPORT_DIR / "statistical_tests.json", statistical)

    git = git_provenance()
    fingerprints = fingerprint_files()
    output_hashes = {}
    for path in sorted(BACKTEST_DIR.glob("*")):
        if path.is_file():
            output_hashes[path.relative_to(ROOT).as_posix()] = sha256_file(str(path))
    base_repeat_paths = [
        BACKTEST_DIR / "portfolio_BASE_repeat_trades.csv", BACKTEST_DIR / "portfolio_BASE_repeat_summary.csv",
        BACKTEST_DIR / "stateful_BASE_repeat_trades.csv", BACKTEST_DIR / "stateful_BASE_repeat_summary.csv",
    ]
    repeat_present = all(path.is_file() for path in base_repeat_paths)
    reproducibility = {
        "status": "PENDING" if not repeat_present else "PASS",
        "reason": "A second BASE output pair for portfolio and stateful engines is required for byte-for-byte reproducibility; run --run-backtests or the documented repeat commands." if not repeat_present else None,
        "repeat_files_present": repeat_present,
    }
    if repeat_present:
        matches = []
        for engine in ("portfolio", "stateful"):
            for suffix in ("trades.csv", "summary.csv"):
                key = f"{engine}_{suffix}"
                first = BACKTEST_DIR / f"{engine}_BASE_{suffix}"
                second = BACKTEST_DIR / f"{engine}_BASE_repeat_{suffix}"
                reproducibility[key] = {"base_sha256": sha256_file(str(first)), "repeat_sha256": sha256_file(str(second)), "match": sha256_file(str(first)) == sha256_file(str(second))}
                matches.append(reproducibility[key]["match"])
        reproducibility["status"] = "PASS" if all(matches) else "FAIL"

    baseline_ref = []
    for scenario, expected in EXPECTED_BASELINE.items():
        row = next((item for item in primary_portfolio if item["scenario"] == scenario and item["threshold"] == 65 and item["mode"] == "LIMIT"), None)
        actual = row["net_pnl_eur"] if row else None
        baseline_ref.append({"scenario": scenario, "expected_net_eur": expected, "reproduced_net_eur": actual, "difference_eur": None if actual is None else round(actual - expected, 8), "matches_within_0_01": actual is not None and abs(actual - expected) <= 0.01})

    pytest_available = importlib.util.find_spec("pytest") is not None
    baseline_test_status = "PENDING" if not pytest_available else "NOT_RUN"
    offline_suite = {"name": "existing_project_pytest_suite", "status": baseline_test_status, "reason": "pytest is not installed in the selected environment; the existing suite could not start." if not pytest_available else "The original suite has not been run in this invocation."}
    phase_tests = {"name": "phase2_1_unittest_suite", "status": "PENDING" if args.skip_offline_tests else "NOT_RUN"}
    if not args.skip_offline_tests:
        phase_tests = run_offline_tests()
    no_lookahead = run_no_lookahead_check()

    expected_hash_ok = inventory["primary_dataset"]["matches_reference"]
    output_check_status = "PASS" if not output_errors and not validation_output_errors and len(portfolio) == 3 and len(stateful) == 3 and len(validation_rows) > 0 else "FAIL"
    baseline_match = all(row["matches_within_0_01"] for row in baseline_ref)
    feature_columns_ok, unexpected_features = validate_feature_columns(primary.columns.tolist())
    unsafe_execution_references = scan_order_execution_calls(ROOT)
    phase1_document = json.loads((ROOT / "reports/phase1_validation.json").read_text(encoding="utf-8"))
    phase1_state = phase1_document.get("overall_status", phase1_document.get("phase1_status", phase1_document.get("status", "UNKNOWN")))
    archived_summary_path = ROOT / "data/backtest/portfolio_summary.csv"
    archived_comparison = []
    if archived_summary_path.is_file():
        archived_summary = pd.read_csv(archived_summary_path)
        for _, archived in archived_summary.iterrows():
            current = next((row for row in primary_portfolio if row["scenario"] == "BASE" and row["threshold"] == int(archived["threshold"]) and row["mode"] == str(archived["mode"])), None)
            archived_comparison.append({
                "dataset": "technical_candidate_scan",
                "threshold": int(archived["threshold"]), "mode": str(archived["mode"]),
                "archived_signals": int(archived["signals_taken"]), "reproduced_signals": current["proposed"] if current else None,
                "signals_difference": current["proposed"] - int(archived["signals_taken"]) if current else None,
                "archived_pnl_eur": float(archived["gross_pnl_eur"]), "reproduced_base_net_eur": current["net_pnl_eur"] if current else None,
                "pnl_difference_eur": round(current["net_pnl_eur"] - float(archived["gross_pnl_eur"]), 8) if current else None,
                "attribution": "UNRESOLVED_ARCHIVED_SUMMARY_DIFFERS_FROM_REPRODUCED_CURRENT_SIMULATOR",
            })
    validation_archived_path = ROOT / "data/backtest/validation_portfolio_summary.csv"
    if validation_archived_path.is_file():
        validation_archived = pd.read_csv(validation_archived_path)
        for _, archived in validation_archived.iterrows():
            current = next((row for row in validation_rows if row.get("engine") == "portfolio" and row.get("scenario") == "BASE" and row.get("threshold") == int(archived["threshold"]) and row.get("mode") == str(archived["mode"])), None)
            archived_comparison.append({
                "dataset": "validation_candidate_scan",
                "threshold": int(archived["threshold"]), "mode": str(archived["mode"]),
                "archived_signals": int(archived["signals_taken"]), "reproduced_signals": current["proposed"] if current else None,
                "signals_difference": current["proposed"] - int(archived["signals_taken"]) if current else None,
                "archived_pnl_eur": float(archived["gross_pnl_eur"]), "reproduced_base_net_eur": current["net_pnl_eur"] if current else None,
                "pnl_difference_eur": round(current["net_pnl_eur"] - float(archived["gross_pnl_eur"]), 8) if current else None,
                "attribution": "UNRESOLVED_ARCHIVED_SUMMARY_DIFFERS_FROM_REPRODUCED_CURRENT_SIMULATOR",
            })
    csv_write(REPORT_DIR / "archived_results_comparison.csv", archived_comparison)
    checks = [
        {"name": "primary_dataset_hash", "status": "PASS" if expected_hash_ok else "FAIL", "evidence": inventory["primary_dataset"]},
        {"name": "offline_phase2_1_tests", "status": phase_tests["status"], "evidence": {"command": phase_tests.get("command"), "return_code": phase_tests.get("return_code"), "duration_seconds": phase_tests.get("duration_seconds"), "output": phase_tests.get("output")}},
        {"name": "existing_project_tests", "status": offline_suite["status"], "evidence": offline_suite},
        {"name": "existing_lookahead_check", "status": no_lookahead["status"], "evidence": no_lookahead},
        {"name": "feature_columns_exclude_outcomes", "status": "PASS" if feature_columns_ok else "FAIL", "evidence": {"unexpected_columns": unexpected_features, "columns": primary.columns.tolist()}},
        {"name": "current_backtest_outputs", "status": output_check_status, "evidence": {"errors": output_errors, "validation_period_errors": validation_output_errors, "scenarios": list(portfolio), "stateful_scenarios": list(stateful), "validation_period_rows": len(validation_rows)}},
        {"name": "baseline_reference_reproduction", "status": "PASS" if baseline_match else "FAIL", "evidence": baseline_ref},
        {"name": "archived_backtest_attribution", "status": "PENDING" if any(row.get("pnl_difference_eur") not in (None, 0) for row in archived_comparison) else "PASS", "evidence": archived_comparison},
        {"name": "base_repeat_reproducibility", "status": reproducibility["status"], "evidence": reproducibility},
        {"name": "deepseek_value_add", "status": "PENDING", "evidence": {"status": llm_audit["status"], "matched_pairs": 0, "reason": llm_audit["reason"]}},
        {"name": "execution_safety", "status": "PASS" if not unsafe_execution_references else "FAIL", "evidence": {"order_send_aliases_or_calls": unsafe_execution_references, "mt5_calls_performed": 0, "provider_api_calls_performed": 0, "order_submission": False, "dry_run_gate_present": "tradeapi_disabled" in (ROOT / "run_dry_pipeline.py").read_text(encoding="utf-8")}},
        {"name": "git_reference_preparation", "status": "PENDING" if not git["reference_commit_available"] or git.get("origin_main") != git.get("commit") else "PASS", "evidence": git},
        {"name": "phase1_closure", "status": "PENDING" if "PENDING" in str(phase1_state).upper() or "INCOMPLETE" in str(phase1_state).upper() else "UNKNOWN", "evidence": {"phase1_status": phase1_state, "report": "reports/phase1_validation.json"}},
    ]
    statuses = {row["status"] for row in checks}
    if "FAIL" in statuses:
        final_state = "PHASE2_1_FAILED"
    elif "PENDING" in statuses or "NOT_RUN" in statuses:
        final_state = "PHASE2_1_INCOMPLETE"
    else:
        final_state = "PHASE2_1_COMPLETE"

    # Conservative conclusion: the predefined criterion needs positive conservative TEST expectancy
    # with an informative weekly-block CI; the available TEST is explored and too short for such CI.
    strategy_conclusion = "INCONCLUSIVE"
    deepseek_conclusion = "INCONCLUSIVE_INSUFFICIENT_DATA"
    execution_commands = backtest_commands or [{"command": "run_portfolio_backtest.py/run_stateful_backtest.py --cost-scenario BASE|CONSERVATIVE|STRESS", "status": "REUSED_AND_HASHED_CURRENT_TASK_OUTPUTS"}]
    validation = {
        "generated_at_utc": utc_now(),
        "phase2_1_status": final_state,
        "strategy_conclusion": strategy_conclusion,
        "deepseek_value_add": deepseek_conclusion,
        "cost_scenarios": COST_SCENARIOS,
        "cost_interpretation": "Historical bid/ask is embedded in execution comparisons; transaction_cost_eur is modeled commission plus slippage. Spread zeros dominate EURUSD/AUDUSD bars, so BASE spread coverage is not verified, while CONSERVATIVE/STRESS are explicit unverified assumptions.",
        "integrity": {"python": sys.version, "python_executable": sys.executable, "pandas": pd.__version__, "numpy": getattr(__import__("numpy"), "__version__", None), "pytest_available": pytest_available, "seed": SEED},
        "provenance": {"git": git, "source_fingerprint_sha256": fingerprints, "dataset_hashes": {"technical_candidate_scan.csv": inventory["primary_dataset"]["sha256"], "validation_candidate_scan.csv": next((x["sha256"] for x in inventory["sources"] if x.get("path") == "data/backtest/validation_candidate_scan.csv"), None)}, "backtest_output_hashes": output_hashes},
        "commands": [sys.argv, *[item.get("command") for item in backtest_commands], *[item.get("command") for item in validation_commands], reconstruction_run.get("command"), no_lookahead.get("command"), phase_tests.get("command")],
        "baseline_reference": baseline_ref,
        "partition": split_info,
        "checks": checks,
        "known_limits": [
            "origin/main could not be fetched due to the environment proxy; work used the isolated current HEAD worktree.",
            "Existing pytest suite could not run because pytest is unavailable.",
            "The primary period is marked as previously explored; chronological TEST results are not confirmatory.",
            "No verifiable event-level DeepSeek or Ollama decision set overlaps the primary opportunities.",
            "MT5 broker, raw tick history, and verified real-world commission/slippage are unavailable; no external integration was called.",
            f"Phase 1 status remains {phase1_state} per its existing validation report.",
        ],
        "commands_to_complete_reproducibility": [
            "python run_portfolio_backtest.py --scan technical_candidate_scan.csv --cost-scenario BASE --name portfolio_BASE_repeat --output-dir reports/phase2_1/backtests",
            "python run_stateful_backtest.py --scan data/backtest/technical_candidate_scan.csv --cost-scenario BASE --name stateful_BASE_repeat --output-dir reports/phase2_1/backtests",
            "python scripts/validate_phase2_1.py",
        ],
        "backtest_execution": execution_commands,
        "validation_period_execution": validation_commands or [{"command": "run_portfolio_backtest.py/run_stateful_backtest.py --scan validation_candidate_scan.csv --cost-scenario BASE|CONSERVATIVE|STRESS", "status": "REUSED_AND_HASHED_CURRENT_TASK_OUTPUTS"}],
    }
    json_write(REPORT_DIR / "validation_summary.json", validation)
    print(json.dumps({"phase2_1_status": final_state, "strategy_conclusion": strategy_conclusion, "deepseek_value_add": deepseek_conclusion, "checks": [{"name": row["name"], "status": row["status"]} for row in checks]}, indent=2))
    return 1 if final_state == "PHASE2_1_FAILED" else 0 if final_state == "PHASE2_1_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
