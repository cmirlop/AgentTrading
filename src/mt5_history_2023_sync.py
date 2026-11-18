"""Read-only MT5 history audit and safe 2023+ reconciliation for the 15 FX CSVs."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from config.market_config import SYMBOLS, TIMEFRAMES
from scripts.validate_mt5_real_readonly import (
    READ_ONLY_METHODS,
    SafetyGateError,
    _attr,
    _read,
    _verify_terminal_gate,
)
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
    safe_copy_rates_range,
    verify_real_readonly_account,
)
from src.mt5_candle_sync import (
    FROZEN_DATASET,
    FROZEN_DATASET_CANONICAL_SHA256,
    TIMEFRAME_API_CONSTANTS,
    _csv_bytes,
    _json_bytes,
    _normalize_rate,
    _pair_id,
    _provider_row,
    _sha256,
    _signature,
    _utc,
    _validate_local,
)


TARGET_START = datetime(2023, 1, 1, tzinfo=timezone.utc)
SCHEMA_VERSION = 1
DEFAULT_BATCH_CALLS = 150
MIN_BATCH_CALLS = 1
MAX_BATCH_CALLS = 1000
MAX_WINDOW_RETRIES = 2
MIN_SPLIT_WINDOW = timedelta(days=1)
MIN_WINDOW_DENSITY = 0.50
EDGE_TOLERANCE = timedelta(days=14)
MAX_D1_CLOSE_GRACE = timedelta(hours=26)
FROZEN_DATASET_PATH = FROZEN_DATASET
PRIVATE_BASE = Path("reports/private/phase2_2_2g")
PUBLIC_JSON = Path("reports/phase2_2_2g/validation_summary.json")
PUBLIC_DOC = Path("docs/FASE2_2_2G_SOLO_VELAS_RESULTADOS.md")
TIMEFRAME_DAYS = {"M5": 365, "M15": 365, "H1": 730, "H4": 1460, "D1": 1460}
REQUIRED_READS = frozenset({"account_info", "symbol_info", "copy_rates_range", "copy_rates_from_pos", "last_error"})
FORBIDDEN_METHODS = frozenset({"order_send", "order_check", "login", "symbol_select"})


class History2023Error(RuntimeError):
    """Safe failure with a stable, non-sensitive error code."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("TIMEZONE_REQUIRED")
    return value.astimezone(timezone.utc).isoformat()


def _plain(value: Any) -> Any:
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _write_json(root: Path, path: Path, payload: dict[str, Any]) -> None:
    safe_atomic_write_bytes(path, _json_bytes(_plain(payload)), authorized_root=root, overwrite=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise History2023Error("PRIVATE_JSON_UNREADABLE") from None
    if not isinstance(value, dict):
        raise History2023Error("PRIVATE_JSON_SCHEMA_INVALID")
    return value


def _window_id(symbol: str, timeframe: str, start: datetime, end: datetime, parent: str | None) -> str:
    identity = f"{symbol}|{timeframe}|{_iso(start)}|{_iso(end)}|{parent or ''}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _pair_state_path(run_root: Path, symbol: str, timeframe: str) -> Path:
    return run_root / "pairs" / f"{symbol}_{timeframe}.json"


def _window_state_path(run_root: Path, window_id: str) -> Path:
    return run_root / "windows" / f"{window_id}.json"


def _chunk_path(run_root: Path, symbol: str, timeframe: str, window_id: str) -> Path:
    return run_root / "provider_chunks" / f"{symbol}_{timeframe}_{window_id}.json"


def _new_window(symbol: str, timeframe: str, start: datetime, end: datetime, parent: str | None = None) -> dict[str, Any]:
    return {
        "window_id": _window_id(symbol, timeframe, start, end, parent),
        "symbol": symbol,
        "timeframe": timeframe,
        "core_start_utc": _iso(start),
        "core_end_exclusive_utc": _iso(end),
        "parent_window_id": parent,
        "status": "PENDING",
        "attempts": 0,
    }


def _seed_year_windows(as_of: datetime, symbol: str, timeframe: str) -> list[dict[str, Any]]:
    end_exclusive = as_of + timedelta(seconds=1)
    step = timedelta(days=TIMEFRAME_DAYS[timeframe])
    windows: list[dict[str, Any]] = []
    cursor = TARGET_START
    while cursor < end_exclusive:
        end = min(cursor + step, end_exclusive)
        windows.append(_new_window(symbol, timeframe, cursor, end))
        cursor = end
    return windows


def _window_bounds(window: dict[str, Any], as_of: datetime) -> tuple[datetime, datetime]:
    start = _utc(window["core_start_utc"])
    end = _utc(window["core_end_exclusive_utc"])
    overlap = timedelta(seconds=TIMEFRAMES_SECONDS[window["timeframe"]])
    return max(TARGET_START, start - overlap), min(as_of, end + overlap)


def _copy_raw_rates(rates: Any) -> list[dict[str, Any]]:
    if rates is None:
        return []
    return [_plain(_record_to_mapping(row)) for row in rates]


def _provider_times(rates: list[dict[str, Any]], timeframe: str) -> tuple[list[datetime], int]:
    times: list[datetime] = []
    invalid = 0
    for row in rates:
        try:
            times.append(_normalize_rate(row)["time"])
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    return times, invalid


def _looks_truncated(window: dict[str, Any], raw_rows: list[dict[str, Any]]) -> bool:
    if not raw_rows:
        return False
    start = _utc(window["core_start_utc"])
    end = _utc(window["core_end_exclusive_utc"])
    duration = end - start
    if duration < timedelta(days=14):
        return False
    times, _ = _provider_times(raw_rows, window["timeframe"])
    if not times:
        return False
    first, last = min(times), max(times)
    max_expected = max(1.0, duration.total_seconds() / TIMEFRAMES_SECONDS[window["timeframe"]])
    density = len(set(times)) / max_expected
    edge = max(EDGE_TOLERANCE, timedelta(seconds=4 * TIMEFRAMES_SECONDS[window["timeframe"]]))
    edge_missing = first > start + edge or last < end - edge
    return density < MIN_WINDOW_DENSITY or edge_missing


def _split_window(window: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    start = _utc(window["core_start_utc"])
    end = _utc(window["core_end_exclusive_utc"])
    if end - start <= MIN_SPLIT_WINDOW * 2:
        return None
    midpoint = start + (end - start) / 2
    # Keep split boundaries on whole seconds so both child queries overlap safely.
    midpoint = midpoint.replace(microsecond=0)
    if midpoint <= start or midpoint >= end:
        return None
    symbol, timeframe, parent = window["symbol"], window["timeframe"], window["window_id"]
    return (
        _new_window(symbol, timeframe, start, midpoint, parent),
        _new_window(symbol, timeframe, midpoint, end, parent),
    )


def _backup_inventory(root: Path, run_root: Path, as_of: datetime) -> dict[str, Any]:
    backup_root = run_root / "original_csv_backup"
    backup_root.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            path = root / "data" / "market" / symbol / f"{timeframe}.csv"
            ensure_no_symlink_path(root, path)
            local = _validate_local(path, timeframe)
            raw = local["raw"]
            backup = backup_root / f"{symbol}_{timeframe}.csv"
            safe_atomic_write_bytes(backup, raw, authorized_root=root, overwrite=False)
            backup_hash = file_content_hashes(backup)
            if backup_hash["raw_sha256"] != local["raw_sha256"]:
                raise History2023Error("ORIGINAL_CSV_BACKUP_HASH_MISMATCH")
            pre2023 = sum(timestamp < TARGET_START for timestamp, _ in local["rows"])
            at_or_after = sum(TARGET_START <= timestamp <= as_of for timestamp, _ in local["rows"])
            files.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "path": path.relative_to(root).as_posix(),
                "backup_path": backup.relative_to(root).as_posix(),
                "rows_before_all_history": local["row_count"],
                "rows_before_2023plus": at_or_after,
                "pre2023_rows": pre2023,
                "first_utc": local["first_utc"],
                "last_utc": local["last_utc"],
                "columns": local["fields"],
                **backup_hash,
            })
    frozen = file_content_hashes(root / FROZEN_DATASET_PATH)
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "target_start_utc": TARGET_START.isoformat(),
        "as_of_utc": as_of.isoformat(),
        "files": files,
        "frozen_dataset": {
            "path": FROZEN_DATASET_PATH,
            "canonical_sha256": frozen["canonical_sha256"],
            "unchanged_expected": frozen["canonical_sha256"] == FROZEN_DATASET_CANONICAL_SHA256,
        },
    }


def _verify_backup_inventory(root: Path, run_root: Path, inventory: dict[str, Any], *, require_sources_unchanged: bool) -> None:
    for item in inventory["files"]:
        backup = root / item["backup_path"]
        source = root / item["path"]
        ensure_no_symlink_path(root, backup)
        ensure_no_symlink_path(root, source)
        if file_content_hashes(backup)["raw_sha256"] != item["raw_sha256"]:
            raise History2023Error("ORIGINAL_CSV_BACKUP_CHANGED")
        if require_sources_unchanged and file_content_hashes(source)["raw_sha256"] != item["raw_sha256"]:
            raise History2023Error("SOURCE_CSV_CHANGED_SINCE_INVENTORY")
    frozen = file_content_hashes(root / FROZEN_DATASET_PATH)
    if frozen["canonical_sha256"] != FROZEN_DATASET_CANONICAL_SHA256:
        raise History2023Error("FROZEN_DATASET_HASH_MISMATCH")


def _initial_manifest(run_id: str, as_of: datetime, inventory: dict[str, Any]) -> dict[str, Any]:
    pairs: dict[str, Any] = {}
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            pairs[_pair_id(symbol, timeframe)] = {
                "symbol": symbol,
                "timeframe": timeframe,
                "visible": None,
                "status": "PENDING",
                "position0_status": "PENDING",
                "position0_open_utc": None,
                "provider_first_utc": None,
                "provider_last_closed_utc": None,
                "provider_rows_raw": 0,
                "provider_rows_closed": 0,
                "invalid_provider_rows": 0,
                "identical_overlap_duplicates": 0,
                "provider_conflicts": 0,
                "range_queries": 0,
                "position_queries": 0,
                "empty_windows": 0,
                "provider_errors": [],
            }
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "PENDING",
        "as_of_utc": as_of.isoformat(),
        "target_start_utc": TARGET_START.isoformat(),
        "account_type": "UNKNOWN",
        "tradeapi_disabled_verified": False,
        "connected_verified": False,
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
        "provider_queries": 0,
        "batch_call_limit": DEFAULT_BATCH_CALLS,
        "files_inventory_path": f"{PRIVATE_BASE.as_posix()}/{run_id}/pre_sync_inventory.json",
        "pairs": pairs,
        "inventory": inventory,
    }


def _load_run(root: Path, run_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    validate_run_id(run_id)
    run_root = root / PRIVATE_BASE / run_id
    ensure_no_symlink_path(root, run_root)
    manifest_path = run_root / "manifest.json"
    inventory_path = run_root / "pre_sync_inventory.json"
    manifest = _read_json(manifest_path)
    inventory = _read_json(inventory_path)
    if manifest.get("run_id") != run_id or manifest.get("schema_version") != SCHEMA_VERSION:
        raise History2023Error("RUN_MANIFEST_ID_OR_SCHEMA_MISMATCH")
    if manifest.get("target_start_utc") != TARGET_START.isoformat():
        raise History2023Error("RUN_TARGET_RANGE_MISMATCH")
    return run_root, manifest, inventory


def _create_run(root: Path, run_id: str, as_of: datetime) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    validate_run_id(run_id)
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("AS_OF_TIMEZONE_REQUIRED")
    as_of = as_of.astimezone(timezone.utc)
    run_root = root / PRIVATE_BASE / run_id
    ensure_no_symlink_path(root, root / PRIVATE_BASE)
    if run_root.exists():
        raise History2023Error("RUN_ID_ALREADY_EXISTS_USE_RESUME")
    run_root.mkdir(parents=True, exist_ok=False)
    for directory in ("windows", "pairs", "provider_chunks"):
        (run_root / directory).mkdir()
    try:
        inventory = _backup_inventory(root, run_root, as_of)
        if not inventory["frozen_dataset"]["unchanged_expected"]:
            raise History2023Error("FROZEN_DATASET_HASH_MISMATCH")
        manifest = _initial_manifest(run_id, as_of, inventory)
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                for window in _seed_year_windows(as_of, symbol, timeframe):
                    _write_json(root, _window_state_path(run_root, window["window_id"]), window)
        _write_json(root, run_root / "pre_sync_inventory.json", inventory)
        _write_json(root, run_root / "manifest.json", manifest)
    except Exception:
        # Keep partial backups/checkpoints for traceability; never remove evidence automatically.
        raise
    return run_root, manifest, inventory


def _timeframe_value(api: Any, timeframe: str) -> Any:
    name = TIMEFRAME_API_CONSTANTS[timeframe]
    value = getattr(api, name, None)
    if value is None:
        raise History2023Error("TIMEFRAME_CONSTANT_UNAVAILABLE")
    return value


def _safe_last_error(api: Any) -> int | None:
    if "last_error" not in READ_ONLY_METHODS:
        return None
    try:
        error = _read(api, "last_error")
        if isinstance(error, (tuple, list)) and error:
            return int(error[0])
        return None
    except Exception:
        return None


def _save_window_result(root: Path, run_root: Path, window: dict[str, Any], raw_rows: list[dict[str, Any]]) -> None:
    path = _chunk_path(run_root, window["symbol"], window["timeframe"], window["window_id"])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "window_id": window["window_id"],
        "symbol": window["symbol"],
        "timeframe": window["timeframe"],
        "request_start_utc": window.get("request_start_utc"),
        "request_end_utc": window.get("request_end_utc"),
        "raw_provider_rows": raw_rows,
    }
    body = _json_bytes(payload)
    safe_atomic_write_bytes(path, body, authorized_root=root, overwrite=False)
    window["chunk_path"] = path.relative_to(root).as_posix()
    window["chunk_sha256"] = _sha256(body)
    window["raw_row_count"] = len(raw_rows)
    times, invalid = _provider_times(raw_rows, window["timeframe"])
    window["invalid_row_count"] = invalid
    window["first_provider_open_utc"] = min(times).isoformat() if times else None
    window["last_provider_open_utc"] = max(times).isoformat() if times else None


def _get_window_rows(root: Path, run_root: Path, window: dict[str, Any]) -> list[dict[str, Any]]:
    relative = window.get("chunk_path")
    if not relative:
        return []
    path = root / relative
    ensure_no_symlink_path(root, path)
    body = path.read_bytes()
    if _sha256(body) != window.get("chunk_sha256"):
        raise History2023Error("PROVIDER_CHUNK_HASH_MISMATCH")
    content = json.loads(body.decode("utf-8"))
    if content.get("window_id") != window.get("window_id"):
        raise History2023Error("PROVIDER_CHUNK_ID_MISMATCH")
    rows = content.get("raw_provider_rows")
    if not isinstance(rows, list):
        raise History2023Error("PROVIDER_CHUNK_ROWS_INVALID")
    return rows


def _window_states(run_root: Path) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = []
    for path in sorted((run_root / "windows").glob("*.json")):
        states.append(_read_json(path))
    return states


def _pair_states(run_root: Path) -> list[dict[str, Any]]:
    return [_read_json(path) for path in sorted((run_root / "pairs").glob("*.json"))]


def _checkpoint(root: Path, run_root: Path, manifest: dict[str, Any], window: dict[str, Any] | None = None, pair: dict[str, Any] | None = None) -> None:
    if window is not None:
        _write_json(root, _window_state_path(run_root, window["window_id"]), window)
    if pair is not None:
        _write_json(root, _pair_state_path(run_root, pair["symbol"], pair["timeframe"]), pair)
    _write_json(root, run_root / "manifest.json", manifest)


def _gate_preflight(api: Any) -> dict[str, Any]:
    gate = _verify_terminal_gate(api)
    account = _read(api, "account_info")
    real_mode = getattr(api, "ACCOUNT_TRADE_MODE_REAL", None)
    if account is None or real_mode is None or _attr(account, "trade_mode") != real_mode:
        raise SafetyGateError("ACCOUNT_NOT_VERIFIED_REAL")
    return {"account_type": "REAL", **gate}


def _process_window(
    api: Any,
    root: Path,
    run_root: Path,
    manifest: dict[str, Any],
    window: dict[str, Any],
    timeframe_value: Any,
) -> tuple[dict[str, Any], bool]:
    as_of = _utc(manifest["as_of_utc"])
    start, end = _window_bounds(window, as_of)
    window["request_start_utc"] = start.isoformat()
    window["request_end_utc"] = end.isoformat()
    symbol = window["symbol"]
    request = {
        "symbol": symbol,
        "request_start_utc": start.isoformat(),
        "request_end_utc": end.isoformat(),
    }
    raw_rates: Any = None
    error_code: int | None = None
    for attempt in range(MAX_WINDOW_RETRIES):
        window["attempts"] = attempt + 1
        manifest["provider_queries"] += 1
        manifest["range_queries"] = int(manifest.get("range_queries", 0)) + 1
        try:
            raw_rates = safe_copy_rates_range(api, request, timeframe_value)
        except SafetyGateError:
            raise
        except Exception:
            error_code = _safe_last_error(api)
            window["provider_error_code"] = error_code
            continue
        if raw_rates is None:
            error_code = _safe_last_error(api)
            window["provider_error_code"] = error_code
            continue
        break
    if raw_rates is None:
        window["status"] = "PROVIDER_ERROR"
        window["provider_error_code"] = error_code
        window["reason"] = "COPY_RATES_RANGE_RETURNED_NONE"
        _checkpoint(root, run_root, manifest, window=window)
        return window, False

    raw_rows = _copy_raw_rates(raw_rates)
    _save_window_result(root, run_root, window, raw_rows)
    if _looks_truncated(window, raw_rows):
        children = _split_window(window)
        if children:
            window["status"] = "SPLIT"
            window["split_reason"] = "PROVIDER_WINDOW_EDGE_OR_DENSITY_INCOMPLETE"
            _checkpoint(root, run_root, manifest, window=window)
            for child in children:
                child_path = _window_state_path(run_root, child["window_id"])
                if not child_path.exists():
                    _write_json(root, child_path, child)
            _write_json(root, run_root / "manifest.json", manifest)
            return window, True
        window["status"] = "PROVIDER_LIMIT"
        window["reason"] = "TRUNCATION_SUSPECTED_AT_MINIMUM_WINDOW"
    elif raw_rows:
        window["status"] = "COMPLETE"
    else:
        window["status"] = "EMPTY"
        window["reason"] = "PROVIDER_RETURNED_NO_ROWS_FOR_WINDOW"
    _checkpoint(root, run_root, manifest, window=window)
    return window, False


def _collect_position0(api: Any, root: Path, run_root: Path, manifest: dict[str, Any], pair: dict[str, Any]) -> None:
    if pair.get("position0_status") != "PENDING":
        return
    symbol, timeframe = pair["symbol"], pair["timeframe"]
    value = _timeframe_value(api, timeframe)
    manifest["provider_queries"] += 1
    manifest["position_queries"] = int(manifest.get("position_queries", 0)) + 1
    try:
        rates = safe_copy_rates_from_pos(api, symbol, value, 0, 1)
    except SafetyGateError:
        raise
    except Exception:
        rates = None
        pair["position0_error_code"] = _safe_last_error(api)
    rows = _copy_raw_rates(rates)
    probe_path = run_root / "provider_chunks" / f"position0_{symbol}_{timeframe}.json"
    body = _json_bytes({"symbol": symbol, "timeframe": timeframe, "raw_provider_rows": rows})
    safe_atomic_write_bytes(probe_path, body, authorized_root=root, overwrite=True)
    pair["position0_chunk_path"] = probe_path.relative_to(root).as_posix()
    pair["position0_chunk_sha256"] = _sha256(body)
    if rows:
        try:
            position = _normalize_rate(rows[0])
            pair["position0_open_utc"] = position["time"].isoformat()
            pair["position0_status"] = "OBSERVED"
        except (KeyError, TypeError, ValueError, OverflowError):
            pair["position0_status"] = "INVALID"
    else:
        pair["position0_status"] = "UNAVAILABLE"
    _checkpoint(root, run_root, manifest, pair=pair)


def _derive_pair_status(run_root: Path, pair: dict[str, Any], as_of: datetime, window_states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    all_states = window_states if window_states is not None else _window_states(run_root)
    states = [state for state in all_states if state["symbol"] == pair["symbol"] and state["timeframe"] == pair["timeframe"]]
    active = [state for state in states if state.get("status") != "SPLIT"]
    pair["window_count"] = len(active)
    pair["complete_windows"] = sum(state.get("status") == "COMPLETE" for state in active)
    pair["empty_windows"] = sum(state.get("status") == "EMPTY" for state in active)
    pair["provider_limit_windows"] = sum(state.get("status") == "PROVIDER_LIMIT" for state in active)
    pair["provider_error_windows"] = sum(state.get("status") == "PROVIDER_ERROR" for state in active)
    pair["range_queries"] = sum(int(state.get("attempts", 0)) for state in states)
    if pair.get("visible") is not True:
        pair["status"] = "BLOCKED_SYMBOL_NOT_VISIBLE"
        return pair
    if pair["provider_limit_windows"] or pair["provider_error_windows"]:
        pair["status"] = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
    elif pair["empty_windows"]:
        pair["status"] = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
    else:
        pair["status"] = "AUDIT_WINDOWS_COMPLETE"
    return pair


def _aggregate_provider(root: Path, run_root: Path, pair: dict[str, Any], as_of: datetime, window_states: list[dict[str, Any]] | None = None) -> tuple[dict[datetime, dict[str, Any]], dict[str, Any]]:
    if pair.get("visible") is not True:
        pair["status"] = "BLOCKED_SYMBOL_NOT_VISIBLE"
        pair["provider_rows_closed"] = 0
        pair["provider_first_utc"] = None
        pair["provider_last_closed_utc"] = None
        return {}, pair
    all_states = window_states if window_states is not None else _window_states(run_root)
    states = [
        state for state in all_states
        if state["symbol"] == pair["symbol"] and state["timeframe"] == pair["timeframe"]
        and state.get("status") in {"COMPLETE", "EMPTY", "PROVIDER_LIMIT", "PROVIDER_ERROR"}
    ]
    candidates: dict[datetime, dict[str, Any]] = {}
    conflicts_by_time: dict[datetime, dict[str, Any]] = {}
    duplicates = 0
    invalid = 0
    raw_count = 0
    for state in states:
        rows = _get_window_rows(root, run_root, state)
        raw_count += len(rows)
        for raw in rows:
            try:
                bar = _normalize_rate(raw)
            except (KeyError, TypeError, ValueError, OverflowError):
                invalid += 1
                continue
            timestamp = bar["time"]
            if timestamp < TARGET_START or timestamp > as_of:
                continue
            existing = candidates.get(timestamp)
            if existing is None:
                candidates[timestamp] = bar
            elif _signature(existing) == _signature(bar):
                duplicates += 1
            else:
                entry = conflicts_by_time.setdefault(timestamp, {
                    "time_utc": timestamp.isoformat(),
                    "kind": "OVERLAP_PROVIDER_CONTENT_CONFLICT",
                    "variants": [list(_signature(existing))],
                })
                signature = list(_signature(bar))
                if signature not in entry["variants"]:
                    entry["variants"].append(signature)

    conflicts = list(conflicts_by_time.values())

    pair["provider_rows_raw"] = raw_count
    pair["invalid_provider_rows"] = invalid
    pair["identical_overlap_duplicates"] = duplicates
    pair["provider_conflicts"] = len(conflicts)
    if conflicts:
        conflict_path = run_root / f"provider_conflicts_{pair['symbol']}_{pair['timeframe']}.json"
        _write_json(root, conflict_path, {
            "symbol": pair["symbol"], "timeframe": pair["timeframe"],
            "conflicts": conflicts,
        })

    position_open = _utc(pair["position0_open_utc"]) if pair.get("position0_open_utc") else None
    all_times = sorted(candidates)
    closed: dict[datetime, dict[str, Any]] = {}
    for index, timestamp in enumerate(all_times):
        if _closed_at_cutoff(timestamp, all_times, index, pair["timeframe"], as_of, position_open):
            closed[timestamp] = candidates[timestamp]

    pair["provider_rows_closed"] = len(closed)
    pair["provider_first_utc"] = min(closed).isoformat() if closed else None
    pair["provider_last_closed_utc"] = max(closed).isoformat() if closed else None
    pair["provider_empty_windows"] = pair.get("empty_windows", 0)
    pair["provider_gap_count_unconfirmed"] = _count_unconfirmed_gaps(closed, pair["timeframe"])
    all_target_rows = len(candidates)
    if invalid or conflicts:
        pair["status"] = "PARTIAL_VALIDATION_OR_CONFLICT"
    elif not closed:
        pair["status"] = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
    elif pair.get("status") == "BLOCKED_SYMBOL_NOT_VISIBLE":
        pass
    elif pair.get("status") == "PARTIAL_PROVIDER_DATA_UNAVAILABLE":
        pass
    else:
        earliest = min(closed)
        latest = max(closed)
        if earliest > TARGET_START + EDGE_TOLERANCE or latest < as_of - EDGE_TOLERANCE:
            pair["status"] = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
        else:
            pair["status"] = "AUDIT_WINDOWS_COMPLETE"
    pair["provider_all_target_rows_before_close_filter"] = all_target_rows
    pair["unconfirmed_empty_windows"] = [
        {"start_utc": state["core_start_utc"], "end_exclusive_utc": state["core_end_exclusive_utc"], "kind": "NO_PROVIDER_DATA"}
        for state in states if state.get("status") == "EMPTY"
    ]
    return closed, pair


def _count_unconfirmed_gaps(bars: dict[datetime, dict[str, Any]], timeframe: str) -> int:
    times = sorted(bars)
    seconds = TIMEFRAMES_SECONDS[timeframe]
    return sum(1 for left, right in zip(times, times[1:]) if (right - left).total_seconds() > seconds * 1.5)


def audit_provider(
    api: Any,
    *,
    root: Path,
    run_id: str,
    as_of_utc: datetime | None = None,
    resume: bool = False,
    batch_call_limit: int = DEFAULT_BATCH_CALLS,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run all finite 2023+ range windows; limits are per batch, never a total cap."""
    if not MIN_BATCH_CALLS <= batch_call_limit <= MAX_BATCH_CALLS:
        raise ValueError("BATCH_CALL_LIMIT_INVALID")
    run_root = root / PRIVATE_BASE / run_id
    if resume:
        run_root, manifest, inventory = _load_run(root, run_id)
        if manifest.get("apply_status") == "APPLIED":
            raise History2023Error("RUN_ALREADY_APPLIED_USE_VERIFY")
        _verify_backup_inventory(root, run_root, inventory, require_sources_unchanged=True)
    else:
        frozen_time = as_of_utc or datetime.now(timezone.utc)
        run_root, manifest, inventory = _create_run(root, run_id, frozen_time)
    event = progress or (lambda _item: None)
    try:
        gate = _gate_preflight(api)
        manifest.update({
            "account_type": gate["account_type"],
            "connected_verified": gate["connected"],
            "tradeapi_disabled_verified": gate["tradeapi_disabled"],
            "status": "IN_PROGRESS",
            "batch_call_limit": batch_call_limit,
        })
        _write_json(root, run_root / "manifest.json", manifest)

        window_cache = _window_states(run_root)
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                pair_path = _pair_state_path(run_root, symbol, timeframe)
                pair = _read_json(pair_path) if pair_path.exists() else manifest["pairs"][_pair_id(symbol, timeframe)]
                if pair.get("visible") is None:
                    try:
                        info = _read(api, "symbol_info", symbol)
                    except SafetyGateError:
                        raise
                    pair["visible"] = _attr(info, "visible") is True if info is not None else False
                    if pair["visible"] is not True:
                        pair["status"] = "BLOCKED_SYMBOL_NOT_VISIBLE"
                        for state in window_cache:
                            if state["symbol"] == symbol and state["timeframe"] == timeframe and state.get("status") == "PENDING":
                                state["status"] = "BLOCKED_SYMBOL_NOT_VISIBLE"
                                state["reason"] = "SYMBOL_NOT_VISIBLE_NOT_SELECTED"
                                _checkpoint(root, run_root, manifest, window=state)
                    _checkpoint(root, run_root, manifest, pair=pair)
                if pair.get("visible") is not True:
                    continue

        calls_in_batch = 0
        pending = [state for state in window_cache if state.get("status") == "PENDING"]
        pending.sort(key=lambda state: (SYMBOLS.index(state["symbol"]), TIMEFRAMES.index(state["timeframe"]), state["core_start_utc"]))
        while True:
            if not pending:
                break
            if calls_in_batch >= batch_call_limit:
                event({"event": "BATCH_COMPLETE", "provider_queries": manifest["provider_queries"], "batch_limit": batch_call_limit, "pending_windows": len(pending)})
                calls_in_batch = 0
            window = pending.pop(0)
            try:
                _process_window(api, root, run_root, manifest, window, _timeframe_value(api, window["timeframe"]))
            except SafetyGateError as exc:
                manifest["status"] = "BLOCKED_SAFETY"
                manifest["safety_error_code"] = str(exc)
                _write_json(root, run_root / "manifest.json", manifest)
                event({"event": "BLOCKED_SAFETY", "error_code": str(exc), "orders_sent": 0, "trades_executed": 0})
                return manifest
            calls_in_batch += max(1, window.get("attempts", 0))
            if window.get("status") == "SPLIT":
                for child in _split_window(window) or ():
                    child_state = _read_json(_window_state_path(run_root, child["window_id"]))
                    window_cache.append(child_state)
                    pending.append(child_state)
                pending.sort(key=lambda state: (SYMBOLS.index(state["symbol"]), TIMEFRAMES.index(state["timeframe"]), state["core_start_utc"]))
            event({
                "event": "WINDOW_CHECKPOINT",
                "provider_queries": manifest["provider_queries"],
                "symbol": window["symbol"],
                "timeframe": window["timeframe"],
                "window_id": window["window_id"],
                "status": window.get("status"),
                "raw_rows": window.get("raw_row_count", 0),
                "pending_windows": len(pending),
            })
            # A safety gate is rechecked after every range read by the wrapper and before the next read.
            _gate_preflight(api)

        # The bar at position 0 is read-only evidence used to exclude the forming candle.
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                pair_path = _pair_state_path(run_root, symbol, timeframe)
                pair = _read_json(pair_path)
                if pair.get("visible") is not True:
                    continue
                before = manifest["provider_queries"]
                try:
                    _collect_position0(api, root, run_root, manifest, pair)
                except SafetyGateError as exc:
                    manifest["status"] = "BLOCKED_SAFETY"
                    manifest["safety_error_code"] = str(exc)
                    _write_json(root, run_root / "manifest.json", manifest)
                    event({"event": "BLOCKED_SAFETY", "error_code": str(exc), "orders_sent": 0, "trades_executed": 0})
                    return manifest
                if manifest["provider_queries"] > before:
                    calls_in_batch += 1
                    if calls_in_batch >= batch_call_limit:
                        event({"event": "BATCH_COMPLETE", "provider_queries": manifest["provider_queries"], "batch_limit": batch_call_limit, "pending_windows": 0})
                        calls_in_batch = 0

        as_of = _utc(manifest["as_of_utc"])
        window_cache = _window_states(run_root)
        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                pair_path = _pair_state_path(run_root, symbol, timeframe)
                pair = _read_json(pair_path)
                _derive_pair_status(run_root, pair, as_of, window_cache)
                _aggregate_provider(root, run_root, pair, as_of, window_cache)
                _checkpoint(root, run_root, manifest, pair=pair)

        pairs = _pair_states(run_root)
        statuses = {pair.get("status") for pair in pairs}
        if "BLOCKED_SYMBOL_NOT_VISIBLE" in statuses:
            overall = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
        elif "PARTIAL_VALIDATION_OR_CONFLICT" in statuses:
            overall = "PARTIAL_VALIDATION_OR_CONFLICT"
        elif "PARTIAL_PROVIDER_DATA_UNAVAILABLE" in statuses:
            overall = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
        elif len(pairs) == len(SYMBOLS) * len(TIMEFRAMES) and all(pair.get("status") == "AUDIT_WINDOWS_COMPLETE" for pair in pairs):
            overall = "COMPLETE_VS_MT5_2023_PLUS"
        else:
            overall = "PARTIAL_EXECUTION_INTERRUPTED"
        manifest["status"] = overall
        manifest["finished_audit_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["orders_sent_by_process"] = 0
        manifest["trades_executed_by_process"] = 0
        _write_json(root, run_root / "manifest.json", manifest)
        return manifest
    except SafetyGateError as exc:
        manifest["status"] = "BLOCKED_SAFETY"
        manifest["safety_error_code"] = str(exc)
        _write_json(root, run_root / "manifest.json", manifest)
        return manifest


def _signature_fields(row: dict[str, Any], fields: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for field in fields:
        value = row[field]
        values.append(str(int(value)) if field == "spread" else format(float(value), ".15g"))
    return tuple(values)


def _closed_at_cutoff(
    timestamp: datetime,
    times: list[datetime],
    index: int,
    timeframe: str,
    as_of: datetime,
    position0_open: datetime | None,
) -> bool:
    if timestamp > as_of:
        return False
    grace = MAX_D1_CLOSE_GRACE if timeframe == "D1" else timedelta(seconds=TIMEFRAMES_SECONDS[timeframe])
    if timestamp == position0_open and timestamp + grace > as_of:
        return False
    if index + 1 < len(times) and times[index + 1] <= as_of:
        return True
    return timestamp + grace <= as_of


def _build_candidate(
    root: Path,
    run_root: Path,
    inventory_item: dict[str, Any],
    pair: dict[str, Any],
    as_of: datetime,
    window_states: list[dict[str, Any]] | None = None,
) -> tuple[bytes | None, dict[str, Any]]:
    symbol, timeframe = inventory_item["symbol"], inventory_item["timeframe"]
    if pair.get("status") == "BLOCKED_SYMBOL_NOT_VISIBLE":
        return None, {
            "symbol": symbol, "timeframe": timeframe,
            "status": "BLOCKED_SYMBOL_NOT_VISIBLE", "error": "SYMBOL_NOT_VISIBLE",
            "rows_before_all_history": inventory_item["rows_before_all_history"],
            "rows_before_2023plus": inventory_item["rows_before_2023plus"],
            "rows_after_2023plus": None, "added_inside_target": 0,
            "pre2023_rows_archived": 0,
        }
    provider, pair = _aggregate_provider(root, run_root, pair, as_of, window_states)
    if pair.get("provider_conflicts"):
        return None, {
            "symbol": symbol, "timeframe": timeframe,
            "status": "PARTIAL_VALIDATION_OR_CONFLICT", "error": "PROVIDER_OVERLAP_CONFLICT",
            "rows_before_all_history": inventory_item["rows_before_all_history"],
            "rows_before_2023plus": inventory_item["rows_before_2023plus"],
            "rows_after_2023plus": None, "added_inside_target": 0,
            "pre2023_rows_archived": 0,
        }
    if pair.get("invalid_provider_rows"):
        invalid_path = run_root / f"invalid_provider_rows_{symbol}_{timeframe}.json"
        _write_json(root, invalid_path, {"symbol": symbol, "timeframe": timeframe, "invalid_count": pair["invalid_provider_rows"]})
        return None, {
            "symbol": symbol, "timeframe": timeframe,
            "status": "PARTIAL_VALIDATION_OR_CONFLICT", "error": "INVALID_PROVIDER_ROWS",
            "rows_before_all_history": inventory_item["rows_before_all_history"],
            "rows_before_2023plus": inventory_item["rows_before_2023plus"],
            "rows_after_2023plus": None, "added_inside_target": 0,
            "pre2023_rows_archived": 0,
        }

    backup = root / inventory_item["backup_path"]
    if file_content_hashes(backup)["raw_sha256"] != inventory_item["raw_sha256"]:
        raise History2023Error("ORIGINAL_CSV_BACKUP_CHANGED")
    local = _validate_local(backup, timeframe)
    local_all_times = [timestamp for timestamp, _ in local["rows"]]
    local_target: dict[datetime, dict[str, Any]] = {}
    local_original_rows = {timestamp: row for timestamp, row in local["rows"]}
    pos0 = _utc(pair["position0_open_utc"]) if pair.get("position0_open_utc") else None
    eligible = [timestamp for timestamp in local_all_times if TARGET_START <= timestamp <= as_of]
    for index, timestamp in enumerate(eligible):
        if not _closed_at_cutoff(timestamp, eligible, index, timeframe, as_of, pos0):
            continue
        local_target[timestamp] = local_original_rows[timestamp]

    conflicts: list[dict[str, Any]] = []
    for timestamp, bar in provider.items():
        old = local_target.get(timestamp)
        if old is None:
            continue
        local_values = {key: old[key] for key in ("open", "high", "low", "close", "tick_volume", "spread", "real_volume")}
        if _signature(local_values) != _signature(bar):
            conflicts.append({"time_utc": timestamp.isoformat(), "kind": "LOCAL_PROVIDER_CONTENT_CONFLICT"})
    if conflicts:
        conflict_path = run_root / f"source_conflicts_{symbol}_{timeframe}.json"
        _write_json(root, conflict_path, {"symbol": symbol, "timeframe": timeframe, "conflicts": conflicts})
        return None, {
            "symbol": symbol, "timeframe": timeframe,
            "status": "PARTIAL_VALIDATION_OR_CONFLICT", "error": "LOCAL_PROVIDER_CONTENT_CONFLICT", "conflicts": len(conflicts),
            "rows_before_all_history": inventory_item["rows_before_all_history"],
            "rows_before_2023plus": inventory_item["rows_before_2023plus"],
            "rows_after_2023plus": None, "added_inside_target": 0,
            "pre2023_rows_archived": 0,
        }

    # Existing valid rows keep their exact string representation; only missing provider rows are serialized.
    merged: dict[datetime, dict[str, Any]] = dict(local_target)
    for timestamp, bar in provider.items():
        merged.setdefault(timestamp, _provider_row(bar, local["fields"]))
    ordered = [merged[timestamp] for timestamp in sorted(merged)]
    raw = _csv_bytes(local["fields"], ordered, "\r\n" if b"\r\n" in local["raw"] else "\n")
    original_target_times = {timestamp for timestamp in local_all_times if TARGET_START <= timestamp <= as_of}
    provider_added = sum(timestamp not in original_target_times for timestamp in provider)
    closed_target_rows = len(ordered)
    pre2023 = inventory_item["pre2023_rows"]
    all_states = window_states if window_states is not None else _window_states(run_root)
    state_by_window = [
        state for state in all_states
        if state["symbol"] == symbol and state["timeframe"] == timeframe
    ]
    empty_windows = sum(state.get("status") == "EMPTY" for state in state_by_window)
    provider_limit_windows = sum(state.get("status") == "PROVIDER_LIMIT" for state in state_by_window)
    provider_errors = sum(state.get("status") == "PROVIDER_ERROR" for state in state_by_window)
    status = pair.get("status", "PENDING")
    if conflicts or pair.get("invalid_provider_rows"):
        status = "PARTIAL_VALIDATION_OR_CONFLICT"
    elif empty_windows or provider_limit_windows or provider_errors or status == "PARTIAL_PROVIDER_DATA_UNAVAILABLE":
        status = "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
    return raw, {
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "rows_before_all_history": inventory_item["rows_before_all_history"],
        "rows_before_2023plus": inventory_item["rows_before_2023plus"],
        "rows_after_2023plus": closed_target_rows,
        "added_inside_target": provider_added,
        "pre2023_rows_archived": pre2023,
        "first_2023plus_utc": min(merged).isoformat() if merged else None,
        "last_closed_utc": max(merged).isoformat() if merged else None,
        "provider_first_utc": pair.get("provider_first_utc"),
        "provider_last_closed_utc": pair.get("provider_last_closed_utc"),
        "provider_rows_observed": pair.get("provider_rows_closed", 0),
        "provider_range_queries": pair.get("range_queries", 0),
        "provider_empty_windows": empty_windows,
        "unconfirmed_intervals": pair.get("provider_gap_count_unconfirmed", 0) + empty_windows,
        "unconfirmed_interval_reason": "session/calendar gaps are not classified without official broker calendar",
        "rows_before_hash_sha256": inventory_item["raw_sha256"],
        "sha256": _sha256(raw),
        "canonical_sha256": _sha256(raw.replace(b"\r\n", b"\n")),
        "columns": local["fields"],
        "conflicts": 0,
        "open_or_future_rows_excluded": max(0, inventory_item["rows_before_2023plus"] - len(local_target)),
    }


def _phase_f_reconciliation(root: Path, new_inventory: dict[str, Any]) -> dict[str, Any] | None:
    old_run_id = "20261115T205056Z"
    old_root = root / "reports" / "private" / "phase2_2_2f" / old_run_id
    old_inventory_path = old_root / "pre_sync_inventory.json"
    if not old_inventory_path.exists():
        return None
    old_inventory = _read_json(old_inventory_path)
    rows_before = sum(int(item.get("row_count", 0)) for item in old_inventory.get("files", []))
    rows_after = sum(int(item["rows_before_all_history"]) for item in new_inventory["files"])
    previous_report_path = root / "reports" / "phase2_2_2f" / "validation_summary.json"
    reported_added = None
    if previous_report_path.exists():
        try:
            previous_report = json.loads(previous_report_path.read_text(encoding="utf-8"))
            reported_added = previous_report.get("total_bars_added")
        except (OSError, json.JSONDecodeError):
            reported_added = None
    return {
        "source": "preserved_phase2_2_2f_pre_sync_inventory_and_2023g_pre_sync_inventory",
        "rows_before_phase_2_2_2f": rows_before,
        "rows_after_phase_2_2_2f_before_phase_g": rows_after,
        "net_row_growth_in_phase_2_2_2f": rows_after - rows_before,
        "phase_2_2_2f_reported_total_bars_added": reported_added,
        "interpretation": "The 989,241 net row growth belongs to phase 2.2.2f and is not counted as bars added in phase 2.2.2g.",
    }


def apply_run(root: Path, run_id: str) -> dict[str, Any]:
    run_root, manifest, inventory = _load_run(root, run_id)
    if manifest.get("status") == "BLOCKED_SAFETY" or manifest.get("account_type") != "REAL" or manifest.get("tradeapi_disabled_verified") is not True or manifest.get("connected_verified") is not True:
        raise SafetyGateError("READ_ONLY_REAL_ACCOUNT_GATE_NOT_VERIFIED_FOR_APPLY")
    if any(state.get("status") == "PENDING" for state in _window_states(run_root)):
        raise History2023Error("PENDING_WINDOWS_REMAIN_USE_RESUME")
    _verify_backup_inventory(root, run_root, inventory, require_sources_unchanged=True)
    as_of = _utc(manifest["as_of_utc"])
    window_states = _window_states(run_root)
    pairs = {(_pair_id(item["symbol"], item["timeframe"])): item for item in _pair_states(run_root)}
    candidates: dict[str, tuple[bytes | None, dict[str, Any]]] = {}
    for item in inventory["files"]:
        pair = pairs[_pair_id(item["symbol"], item["timeframe"])]
        payload, report = _build_candidate(root, run_root, item, pair, as_of, window_states)
        candidates[item["path"]] = (payload, report)

    # Build and validate every eligible file before the first operational write.
    final_hashes: dict[str, str] = {}
    for item in inventory["files"]:
        path = root / item["path"]
        payload, report = candidates[item["path"]]
        if payload is None:
            report["sha256"] = file_content_hashes(path)["raw_sha256"]
            report["rows_after_2023plus"] = None
            report["rows_after_all_history_untouched"] = item["rows_before_all_history"]
            report["pre2023_rows_archived"] = 0
            report["added_inside_target"] = 0
            continue
        if _sha256(payload) != report["sha256"]:
            raise History2023Error("CANDIDATE_HASH_MISMATCH")
        final_hashes[item["path"]] = report["sha256"]

    for item in inventory["files"]:
        path = root / item["path"]
        payload, report = candidates[item["path"]]
        if payload is None:
            continue
        if file_content_hashes(path)["raw_sha256"] != item["raw_sha256"]:
            raise History2023Error("SOURCE_CSV_CHANGED_DURING_APPLY")
        if path.read_bytes() != payload:
            safe_atomic_write_bytes(path, payload, authorized_root=root, overwrite=True)
        if file_content_hashes(path)["raw_sha256"] != report["sha256"]:
            raise History2023Error("APPLIED_CSV_HASH_MISMATCH")

    _verify_backup_inventory(root, run_root, inventory, require_sources_unchanged=False)
    manifest["apply_status"] = "APPLIED"
    manifest["apply_completed_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["final_hashes"] = final_hashes
    manifest["orders_sent_by_process"] = 0
    manifest["trades_executed_by_process"] = 0
    _write_json(root, run_root / "manifest.json", manifest)
    items = [report for _, report in candidates.values()]
    result = {
        "status": _global_status(manifest, items),
        "run_id": run_id,
        "as_of_utc": manifest["as_of_utc"],
        "provider_queries": manifest["provider_queries"],
        "idempotency_second_pass": None,
        "idempotency_second_pass_new_bars": None,
        "items": items,
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
    }
    summary = public_summary(root, run_id, result)
    write_public_reports(root, summary)
    return summary


def _global_status(manifest: dict[str, Any], items: list[dict[str, Any]]) -> str:
    if manifest.get("status") == "BLOCKED_SAFETY":
        return "BLOCKED_SAFETY"
    statuses = [item.get("status") for item in items]
    if "PARTIAL_VALIDATION_OR_CONFLICT" in statuses:
        return "PARTIAL_VALIDATION_OR_CONFLICT"
    if "PARTIAL_PROVIDER_DATA_UNAVAILABLE" in statuses or "BLOCKED_SYMBOL_NOT_VISIBLE" in statuses:
        return "PARTIAL_PROVIDER_DATA_UNAVAILABLE"
    if len(statuses) == len(SYMBOLS) * len(TIMEFRAMES) and all(status == "AUDIT_WINDOWS_COMPLETE" for status in statuses):
        return "COMPLETE_VS_MT5_2023_PLUS"
    return "PARTIAL_EXECUTION_INTERRUPTED"


def verify_run(root: Path, run_id: str) -> dict[str, Any]:
    run_root, manifest, inventory = _load_run(root, run_id)
    if manifest.get("apply_status") != "APPLIED":
        raise History2023Error("RUN_NOT_APPLIED_USE_APPLY")
    as_of = _utc(manifest["as_of_utc"])
    window_states = _window_states(run_root)
    pairs = {(_pair_id(item["symbol"], item["timeframe"])): item for item in _pair_states(run_root)}
    items: list[dict[str, Any]] = []
    unchanged = True
    for item in inventory["files"]:
        path = root / item["path"]
        current = file_content_hashes(path)["raw_sha256"]
        expected = manifest.get("final_hashes", {}).get(item["path"])
        pair = pairs[_pair_id(item["symbol"], item["timeframe"])]
        payload, report = _build_candidate(root, run_root, item, pair, as_of, window_states)
        if payload is None:
            expected = current
        if current != expected or (payload is not None and _sha256(payload) != current):
            unchanged = False
            report["status"] = "PARTIAL_VALIDATION_OR_CONFLICT"
            report["error"] = "SECOND_PASS_HASH_MISMATCH"
        report["symbol"] = item["symbol"]
        report["timeframe"] = item["timeframe"]
        report["second_pass_new_bars"] = 0 if unchanged and current == expected else None
        report["sha256"] = current
        items.append(report)
    _verify_backup_inventory(root, run_root, inventory, require_sources_unchanged=False)
    manifest["verify_completed_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["idempotency_second_pass"] = unchanged
    manifest["idempotency_second_pass_new_bars"] = 0 if unchanged else None
    _write_json(root, run_root / "manifest.json", manifest)
    result = {
        "status": _global_status(manifest, items) if unchanged else "PARTIAL_VALIDATION_OR_CONFLICT",
        "run_id": run_id,
        "as_of_utc": manifest["as_of_utc"],
        "provider_queries": manifest["provider_queries"],
        "idempotency_second_pass": unchanged,
        "idempotency_second_pass_new_bars": 0 if unchanged else None,
        "items": items,
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
    }
    summary = public_summary(root, run_id, result)
    write_public_reports(root, summary)
    return summary


def public_summary(root: Path, run_id: str, operation: dict[str, Any] | None = None) -> dict[str, Any]:
    run_root, manifest, inventory = _load_run(root, run_id)
    pairs = {(_pair_id(item["symbol"], item["timeframe"])): item for item in _pair_states(run_root)}
    window_states = _window_states(run_root)
    operation_map = {
        (item.get("symbol"), item.get("timeframe")): item
        for item in (operation or {}).get("items", [])
    }
    summary_items: list[dict[str, Any]] = []
    for source in inventory["files"]:
        symbol, timeframe = source["symbol"], source["timeframe"]
        pair = pairs[_pair_id(symbol, timeframe)]
        op = operation_map.get((symbol, timeframe), {})
        states = [state for state in window_states if state["symbol"] == symbol and state["timeframe"] == timeframe]
        rows = op.get("rows_after_2023plus", source["rows_before_2023plus"])
        summary_items.append({
            "symbol": symbol,
            "timeframe": timeframe,
            "first_2023plus_utc": op.get("first_2023plus_utc", pair.get("provider_first_utc")),
            "last_closed_utc": op.get("last_closed_utc", pair.get("provider_last_closed_utc")),
            "effective_cutoff_utc": op.get("last_closed_utc", pair.get("provider_last_closed_utc")),
            "rows_before_all_history": source["rows_before_all_history"],
            "rows_before_2023plus": source["rows_before_2023plus"],
            "rows_after_2023plus": rows,
            "added_inside_target": op.get("added_inside_target", 0),
            "pre2023_rows_archived": op.get("pre2023_rows_archived", 0),
            "provider_coverage": {
                "first_open_utc": pair.get("provider_first_utc"),
                "last_closed_open_utc": pair.get("provider_last_closed_utc"),
                "rows_closed_observed": pair.get("provider_rows_closed", 0),
                "range_windows": len(states),
                "completed_windows": pair.get("complete_windows", 0),
                "empty_windows": pair.get("empty_windows", 0),
                "provider_limit_windows": pair.get("provider_limit_windows", 0),
                "range_queries": pair.get("range_queries", 0),
                "position_queries": pair.get("position_queries", 0),
            },
            "unconfirmed_intervals": op.get("unconfirmed_intervals", pair.get("provider_gap_count_unconfirmed", 0) + pair.get("empty_windows", 0)),
            "status": op.get("status", pair.get("status", "PENDING")),
            "sha256": op.get("sha256") or source["raw_sha256"],
            "provider_rows_added": op.get("added_inside_target", 0),
            "open_or_future_rows_excluded": op.get("open_or_future_rows_excluded", 0),
        })
    rows_added = sum(int(item["added_inside_target"]) for item in summary_items)
    archived = sum(int(item["pre2023_rows_archived"]) for item in summary_items)
    final_rows = sum(int(item["rows_after_2023plus"]) for item in summary_items if item["rows_after_2023plus"] is not None)
    as_of = manifest["as_of_utc"]
    if operation and operation.get("status"):
        status = operation["status"]
    elif manifest.get("status") == "BLOCKED_SAFETY":
        status = "BLOCKED_SAFETY"
    elif manifest.get("status") == "PARTIAL_EXECUTION_INTERRUPTED":
        status = "PARTIAL_EXECUTION_INTERRUPTED"
    else:
        status = manifest.get("status", "PENDING")
    reconciliation = _phase_f_reconciliation(root, inventory)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "run_id": run_id,
        "target_start_utc": TARGET_START.isoformat(),
        "as_of_utc": as_of,
        "account_type": manifest.get("account_type", "UNKNOWN"),
        "read_only_safety": "PASS" if manifest.get("tradeapi_disabled_verified") is True and manifest.get("connected_verified") is True else ("FAIL" if status == "BLOCKED_SAFETY" else "PENDING"),
        "provider_queries": manifest.get("provider_queries", 0),
        "range_queries": manifest.get("range_queries", 0),
        "position_queries": manifest.get("position_queries", 0),
        "batch_call_limit": manifest.get("batch_call_limit", DEFAULT_BATCH_CALLS),
        "global_query_cap": None,
        "terminal_maxbars_observed": manifest.get("terminal_maxbars_observed"),
        "frozen_dataset_unchanged": inventory.get("frozen_dataset", {}).get("unchanged_expected") is True,
        "frozen_dataset_canonical_sha256": inventory.get("frozen_dataset", {}).get("canonical_sha256"),
        "aggregate": {
            "pre2023_rows_archived": archived,
            "new_2023plus_rows": rows_added,
            "final_2023plus_rows": final_rows,
        },
        "phase2_2_2f_reconciliation": reconciliation,
        "idempotency_second_pass": (operation or {}).get("idempotency_second_pass"),
        "idempotency_second_pass_new_bars": (operation or {}).get("idempotency_second_pass_new_bars"),
        "orders_sent_by_process": 0,
        "trades_executed_by_process": 0,
        "private_evidence_path": f"{PRIVATE_BASE.as_posix()}/{run_id}",
        "items": summary_items,
    }


def write_public_reports(root: Path, summary: dict[str, Any]) -> None:
    report_dir = root / "reports" / "phase2_2_2g"
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_json(root, root / PUBLIC_JSON, summary)
    header = "| Símbolo | TF | Primera 2023+ UTC | Último cierre UTC | Filas antes (todo) | Filas antes (2023+) | Filas finales (2023+) | Añadidas 2023+ | Pre-2023 archivadas | Cobertura proveedor | Intervalos sin confirmar | Estado | SHA-256 |"
    divider = "|---|---|---|---|---:|---:|---:|---:|---:|---|---:|---|---|"
    rows = [header, divider]
    for item in summary["items"]:
        coverage = item["provider_coverage"]
        provider_text = f"{coverage['first_open_utc'] or '—'} → {coverage['last_closed_open_utc'] or '—'} ({coverage['rows_closed_observed']} barras)"
        rows_after = item["rows_after_2023plus"] if item["rows_after_2023plus"] is not None else "— (CSV intacto por bloqueo)"
        rows.append(
            f"| {item['symbol']} | {item['timeframe']} | {item['first_2023plus_utc'] or '—'} | {item['last_closed_utc'] or '—'} | {item['rows_before_all_history']} | {item['rows_before_2023plus']} | {rows_after} | {item['added_inside_target']} | {item['pre2023_rows_archived']} | {provider_text} | {item['unconfirmed_intervals']} | {item['status']} | `{item['sha256']}` |"
        )
    reconciliation = summary.get("phase2_2_2f_reconciliation")
    recon_text = "No se pudo verificar la conciliación de fase 2.2.2f con el inventario preservado." if reconciliation is None else (
        f"Fase 2.2.2f: {reconciliation['rows_before_phase_2_2_2f']:,} filas al inicio y "
        f"{reconciliation['rows_after_phase_2_2_2f_before_phase_g']:,} al cierre previo a esta fase; "
        f"crecimiento neto verificado {reconciliation['net_row_growth_in_phase_2_2_2f']:,}. "
        "Ese crecimiento previo no se cuenta como filas nuevas de fase 2.2.2g."
    )
    aggregate = summary["aggregate"]
    doc = [
        "# Fase 2.2.2g — Histórico MT5 desde 2023",
        "",
        f"**Estado:** {summary['status']}",
        "",
        f"Rango solicitado: `{summary['target_start_utc']}` hasta el corte UTC inmutable `{summary['as_of_utc']}`. La fecha de apertura determina la inclusión. Las velas en formación se excluyen; D1 usa la siguiente apertura observada o una gracia conservadora de 26 horas, no un cierre fijo de 86.400 s.",
        "",
        f"Cuenta REAL, terminal conectado y `tradeapi_disabled=True`: **{summary['read_only_safety']}**. El proceso envió 0 órdenes y ejecutó 0 operaciones. El dataset congelado permanece intacto: **{summary['frozen_dataset_unchanged']}** (`{summary['frozen_dataset_canonical_sha256']}`).",
        "",
        f"Totales comprobados: {aggregate['pre2023_rows_archived']:,} filas pre-2023 archivadas en los backups privados; {aggregate['new_2023plus_rows']:,} filas 2023+ añadidas respecto al estado inicial de esta fase; {aggregate['final_2023plus_rows']:,} filas finales en los 15 CSV operativos.",
        "",
        recon_text,
        "",
        f"Consultas MT5: {summary['provider_queries']} en total, con límite de {summary['batch_call_limit']} por tanda y sin límite global fijo. Cobertura/filas observadas no certifican sesiones oficiales del bróker. Los intervalos sin confirmar incluyen períodos sin ticks y no se rellenan ni se etiquetan como errores de sesión automáticamente.",
        "",
        header,
        divider,
        *rows[2:],
        "",
        f"Idempotencia segundo pase: `{summary['idempotency_second_pass']}`; nuevas filas en ese pase: `{summary['idempotency_second_pass_new_bars']}`.",
        f"Evidencia privada (backups originales, manifiesto, ventanas y chunks): `{summary['private_evidence_path']}`.",
        "",
        "## Comandos para retomar o verificar",
        "",
        "```powershell",
        "python scripts/sync_mt5_history_2023_readonly.py --audit --run-id <nuevo-id>",
        "python scripts/sync_mt5_history_2023_readonly.py --resume --run-id <mismo-id>",
        "python scripts/sync_mt5_history_2023_readonly.py --apply --run-id <mismo-id>",
        "python scripts/sync_mt5_history_2023_readonly.py --verify --run-id <mismo-id>",
        "```",
        "",
        "`--resume` conserva el `as_of_utc` del manifiesto. Para un corte posterior, iniciar un run nuevo. No ejecutar ni modificar símbolos, órdenes, posiciones, estrategia o terminal.",
        "",
    ]
    safe_atomic_write_bytes(root / PUBLIC_DOC, "\n".join(doc).encode("utf-8"), authorized_root=root, overwrite=True)

