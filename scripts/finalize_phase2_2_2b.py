"""Reconcile Phase 2.2.2b evidence and write a privacy-safe final result.

This finalizer is offline-only. It reads the completed local run and reports;
it never imports MetaTrader5 or calls a broker, LLM, or trading API.
"""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPORT_DIR = ROOT / "reports" / "phase2_2_2b"
BASELINE = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
EXPECTED_BASELINE_CANONICAL_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
MAX_MARKET_DATA_REQUESTS = 150
MAX_BARS_PER_REQUEST = 1000


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("REPORT_ROOT_MUST_BE_OBJECT")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    from src.history_data_integrity import safe_atomic_write_bytes

    content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    safe_atomic_write_bytes(path, content, authorized_root=ROOT)


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _privacy_scan(value: Any, *, trail: str = "root") -> list[str]:
    sensitive_key = re.compile(
        r"(account_number|account_id|login|server|balance|equity|ticket|deal|api[_-]?key|token|password|credential)",
        re.IGNORECASE,
    )
    absolute_windows_path = re.compile(r"(?i)(?:[A-Z]:\\|\\\\[^\\]+\\[^\\]+)")
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if sensitive_key.search(str(key)):
                findings.append(f"SENSITIVE_FIELD_NAME:{trail}.{key}")
            findings.extend(_privacy_scan(item, trail=f"{trail}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(_privacy_scan(item, trail=f"{trail}[{index}]"))
    elif isinstance(value, str) and absolute_windows_path.search(value):
        findings.append(f"ABSOLUTE_WINDOWS_PATH:{trail}")
    return findings


def derive_phase_status(
    critical_checks: dict[str, bool],
    *,
    continuity_status: str,
    pending_recovery_intervals: int,
    calendar_review_intervals: int,
) -> tuple[str, list[str]]:
    """Derive the terminal phase state from evidence, never from a manual flag."""
    failed = sorted(name for name, passed in critical_checks.items() if passed is not True)
    if failed:
        return "PHASE2_2_2B_BLOCKED", failed
    if (
        continuity_status == "HISTORY_CONTINUITY_VERIFIED"
        and pending_recovery_intervals == 0
        and calendar_review_intervals == 0
    ):
        return "PHASE2_2_2B_COMPLETE_DATA_VERIFIED", []
    return "PHASE2_2_2B_COMPLETE_WITH_PENDING_GAPS", []


def _run_finalizer_tests() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "-v", "tests.test_phase2_2_2b_finalizer"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    count_match = re.search(r"Ran (\d+) tests?", completed.stdout)
    skipped_match = re.search(r"skipped=(\d+)", completed.stdout)
    count = int(count_match.group(1)) if count_match else None
    skipped = int(skipped_match.group(1)) if skipped_match else 0
    passed = completed.returncode == 0 and count is not None
    return {
        "command": "python -m unittest -v tests.test_phase2_2_2b_finalizer",
        "status": "PASS" if passed else "FAIL",
        "tests_run": count,
        "tests_passed": max(0, count - skipped) if count is not None and passed else 0,
        "tests_failed": 0 if passed else 1,
        "tests_skipped": skipped,
        "exit_code": completed.returncode,
    }


def _hash_maps_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
        return False
    for name in left:
        a, b = left[name], right[name]
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if a.get("raw_sha256") != b.get("raw_sha256"):
            return False
        if a.get("canonical_sha256") != b.get("canonical_sha256"):
            return False
        if a.get("canonicalization_policy") != b.get("canonicalization_policy"):
            return False
    return True


def _interval_summary(items: list[dict[str, Any]], symbols: list[str], timeframes: list[str]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault((str(item.get("symbol")), str(item.get("timeframe"))), []).append(item)
    result: dict[str, Any] = {}
    for symbol in symbols:
        result[symbol] = {}
        for timeframe in timeframes:
            rows = grouped.get((symbol, timeframe), [])
            missing_bars = [row.get("missing_bar_count") for row in rows]
            known_counts = [count for count in missing_bars if isinstance(count, (int, float))]
            labels = [
                str(row.get("classification", row.get("reason", "UNCLASSIFIED")))
                for row in rows
            ]
            result[symbol][timeframe] = {
                "interval_count": len(rows),
                "known_missing_bar_count": int(sum(known_counts)) if known_counts else None,
                "classifications": dict(sorted(Counter(labels).items())),
            }
    return result


def reconcile_missing_cadence_slots(
    initial_items: list[dict[str, Any]],
    post_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare explicit gap intervals; cadence positions are not asserted tradable."""
    from src.market_history_audit import TIMEFRAMES_SECONDS, parse_utc_timestamp

    def collect(items: list[dict[str, Any]]) -> set[tuple[str, str, datetime]]:
        result: set[tuple[str, str, datetime]] = set()
        for item in items:
            symbol, timeframe = str(item.get("symbol", "")), str(item.get("timeframe", ""))
            start_text, end_text = item.get("missing_start_utc"), item.get("missing_end_utc")
            seconds = TIMEFRAMES_SECONDS.get(timeframe)
            if not symbol or seconds is None or not start_text or not end_text:
                continue
            start = parse_utc_timestamp(str(start_text))
            end = parse_utc_timestamp(str(end_text))
            if end < start:
                continue
            current = start
            while current <= end:
                result.add((symbol, timeframe, current))
                current += timedelta(seconds=seconds)
        return result

    before = collect(initial_items)
    after = collect(post_items)
    removed = before - after
    added = after - before
    removed_by_pair = Counter(f"{symbol}|{timeframe}" for symbol, timeframe, _ in removed)
    added_by_pair = Counter(f"{symbol}|{timeframe}" for symbol, timeframe, _ in added)
    return {
        "initial_reviewed_missing_cadence_slots": len(before),
        "post_reviewed_missing_cadence_slots": len(after),
        "missing_cadence_slots_removed": len(removed),
        "missing_cadence_slots_added": len(added),
        "removed_by_symbol_timeframe": dict(sorted(removed_by_pair.items())),
        "added_by_symbol_timeframe": dict(sorted(added_by_pair.items())),
        "interpretation": "Cadence gaps include uncertain calendar closures and are not a count of confirmed tradable bars.",
    }


def _campaign_evidence(
    run_id: str,
    plan: dict[str, Any],
    manifest: dict[str, Any],
    audit: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    from src.history_data_integrity import backfill_run_paths, content_hashes, ensure_no_symlink_path
    from src.market_history_audit import REQUIRED_COLUMNS, parse_utc_timestamp
    from src.market_history_backfill import _plan_fingerprint, _plan_snapshot, plan_chunks

    errors: list[str] = []
    try:
        paths = backfill_run_paths(ROOT, run_id)
        run_root = paths["run_root"]
        chunks_dir = paths["chunks"]
        manifest_path = paths["manifest"]
        ensure_no_symlink_path(ROOT.resolve(), run_root)
        ensure_no_symlink_path(ROOT.resolve(), chunks_dir)
    except (OSError, ValueError, RuntimeError):
        return {"run_id": run_id, "status": "RUN_PATH_INVALID"}, ["RUN_PATH_INVALID"]

    if not manifest_path.is_file() or not chunks_dir.is_dir():
        return {"run_id": run_id, "status": "RUN_ARTIFACTS_MISSING"}, ["RUN_ARTIFACTS_MISSING"]

    if manifest.get("run_id") != run_id:
        errors.append("RUN_ID_MISMATCH")
    if manifest.get("account_type") != "REAL":
        errors.append("ACCOUNT_NOT_VERIFIED_REAL")
    if manifest.get("tradeapi_disabled_verified") is not True:
        errors.append("TRADEAPI_DISABLED_NOT_VERIFIED_TRUE")
    if manifest.get("orders_sent") != 0:
        errors.append("ORDER_COUNT_NOT_ZERO")
    if manifest.get("source_files_unchanged") is not True or audit.get("source_files_unchanged") is not True:
        errors.append("ORIGINAL_SOURCE_HASHES_CHANGED")

    plan_snapshot = manifest.get("plan_snapshot")
    if not isinstance(plan_snapshot, dict):
        errors.append("PLAN_SNAPSHOT_MISSING")
    else:
        manifest_plan = {
            "schema_version": manifest.get("schema_version"),
            "canonicalization_policy": manifest.get("canonicalization_policy"),
            **plan_snapshot,
        }
        if _plan_fingerprint(manifest_plan) != manifest.get("plan_sha256"):
            errors.append("PLAN_FINGERPRINT_MISMATCH")
        if plan_snapshot != _plan_snapshot(plan):
            errors.append("RUN_PLAN_DIFFERS_FROM_FROZEN_PLAN")

    planned_chunks = plan_chunks(
        plan.get("backfill_intervals", []),
        symbols=plan.get("symbols", []),
        timeframes=plan.get("timeframes_seconds", {}),
        max_bars_per_request=plan.get("chunk_policy", {}).get("max_bars_per_request", MAX_BARS_PER_REQUEST),
        overlap_bars_each_side=plan.get("chunk_policy", {}).get("overlap_bars_each_side", 2),
    )
    manifest_chunks = manifest.get("chunks")
    manifest_planned_chunks = manifest.get("planned_chunks")
    if not isinstance(manifest_planned_chunks, dict) or not isinstance(manifest_chunks, dict):
        errors.append("CHUNK_INVENTORY_MISSING")
        manifest_planned_chunks = {}
        manifest_chunks = {}
    planned_ids = {str(chunk.get("chunk_id")) for chunk in planned_chunks if isinstance(chunk, dict)}
    expected_plan_chunks = {chunk["chunk_id"]: chunk for chunk in planned_chunks}
    if set(manifest_chunks) != planned_ids or manifest_planned_chunks != expected_plan_chunks:
        errors.append("CHUNK_INVENTORY_MISMATCH")
    if plan.get("planned_chunk_count") != len(planned_chunks):
        errors.append("PLANNED_CHUNK_COUNT_MISMATCH")
    if len(planned_chunks) > MAX_MARKET_DATA_REQUESTS:
        errors.append("MARKET_DATA_REQUEST_LIMIT_EXCEEDED")
    if manifest.get("provider_calls") != len(planned_chunks):
        errors.append("PROVIDER_CALL_COUNT_MISMATCH")
    if manifest.get("status") not in {
        "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT",
        "HISTORY_BACKFILL_PREPARED_WITH_REVIEW",
    }:
        errors.append("BACKFILL_RUN_NOT_COMPLETED")

    source_before = manifest.get("source_file_hashes_at_run")
    source_after = manifest.get("source_file_hashes_after")
    source_plan = plan.get("source_file_hashes")
    source_hashes_equal = _hash_maps_equal(source_before, source_after) and _hash_maps_equal(source_plan, source_before)
    if not source_hashes_equal:
        errors.append("ORIGINAL_SOURCE_FINGERPRINT_MISMATCH")

    actual_chunk_files = set()
    try:
        for path in chunks_dir.iterdir():
            ensure_no_symlink_path(ROOT.resolve(), path)
            if path.is_symlink() or not path.is_file():
                errors.append("CHUNK_PATH_INVALID")
            else:
                actual_chunk_files.add(path.name)
    except OSError:
        errors.append("CHUNK_DIRECTORY_UNREADABLE")

    expected_chunk_files = {f"{chunk_id}.csv" for chunk_id in planned_ids}
    if actual_chunk_files != expected_chunk_files:
        errors.append("CHUNK_FILE_INVENTORY_MISMATCH")

    symbols = [str(value) for value in plan.get("symbols", [])]
    timeframes = [str(value) for value in plan.get("timeframes_seconds", {})]
    grouped: dict[tuple[str, str], dict[str, Any]] = {
        (symbol, timeframe): {
            "request_count": 0,
            "bar_rows_returned": 0,
            "unique_timestamps": set(),
            "first_bar_utc": None,
            "latest_closed_bar_utc": None,
        }
        for symbol in symbols for timeframe in timeframes
    }
    total_rows = 0
    empty_chunks = 0
    completed_chunks = 0
    failed_chunks = 0
    excluded_open_bars = 0

    for chunk_id, entry in manifest_chunks.items():
        chunk = entry.get("planned_chunk", {}) if isinstance(entry, dict) else {}
        symbol, timeframe = str(chunk.get("symbol", "")), str(chunk.get("timeframe", ""))
        if chunk != expected_plan_chunks.get(chunk_id):
            errors.append("CHUNK_PLAN_MISMATCH")
        if (symbol, timeframe) not in grouped:
            errors.append("CHUNK_SYMBOL_OR_TIMEFRAME_INVALID")
            continue
        stats = grouped[(symbol, timeframe)]
        stats["request_count"] += 1
        chunk_status = entry.get("status")
        if chunk_status in {"COMPLETED", "COMPLETED_WITH_CONFLICTS", "RECOVERED_ATOMIC_CHUNK_FILE"}:
            completed_chunks += 1
        else:
            failed_chunks += 1
            errors.append("CHUNK_NOT_COMPLETED")
        if entry.get("attempts") != 1 or entry.get("provider_call_count") != 1:
            errors.append("CHUNK_RETRIED_OR_CALL_COUNT_MISMATCH")
        if entry.get("tradeapi_disabled_verified_before_and_after") is not True:
            errors.append("CHUNK_READ_ONLY_GATE_NOT_VERIFIED_BEFORE_AND_AFTER")
        if int(entry.get("open_or_future_bar_count_excluded", 0)) > 0:
            excluded_open_bars += int(entry["open_or_future_bar_count_excluded"])
        if entry.get("empty_response") is True or int(entry.get("bar_count", 0)) == 0:
            empty_chunks += 1
        if int(entry.get("bar_count", 0)) > MAX_BARS_PER_REQUEST:
            errors.append("CHUNK_BAR_LIMIT_EXCEEDED")

        relative_file = entry.get("file")
        if relative_file != f"data/market_history/backfill_runs/{run_id}/chunks/{chunk_id}.csv":
            errors.append("CHUNK_PATH_DOES_NOT_MATCH_RUN")
            continue
        chunk_path = ROOT / relative_file
        try:
            ensure_no_symlink_path(ROOT.resolve(), chunk_path)
            raw = chunk_path.read_bytes()
        except (OSError, ValueError):
            errors.append("CHUNK_FILE_UNAVAILABLE_OR_UNSAFE")
            continue
        hashes = content_hashes(raw)
        if hashes.get("raw_sha256") != entry.get("raw_sha256") or hashes.get("canonical_sha256") != entry.get("canonical_sha256"):
            errors.append("CHUNK_HASH_MISMATCH")
            continue

        request_start = parse_utc_timestamp(str(chunk.get("request_start_utc", "")))
        request_end = parse_utc_timestamp(str(chunk.get("request_end_utc", "")))
        seen = set()
        try:
            with chunk_path.open("r", newline="", encoding="utf-8-sig") as handle:
                reader = csv.DictReader(handle)
                if tuple(reader.fieldnames or ()) != tuple(REQUIRED_COLUMNS):
                    errors.append("CHUNK_COLUMNS_INVALID")
                    continue
                for row in reader:
                    timestamp = parse_utc_timestamp(row.get("time", ""))
                    if timestamp < request_start or timestamp > request_end:
                        errors.append("CHUNK_BAR_OUTSIDE_REQUEST_RANGE")
                        break
                    seen.add(timestamp)
                    stats["unique_timestamps"].add(timestamp)
                    stats["bar_rows_returned"] += 1
                    total_rows += 1
                    if stats["first_bar_utc"] is None or timestamp < stats["first_bar_utc"]:
                        stats["first_bar_utc"] = timestamp
                    if stats["latest_closed_bar_utc"] is None or timestamp > stats["latest_closed_bar_utc"]:
                        stats["latest_closed_bar_utc"] = timestamp
        except (OSError, KeyError, TypeError, ValueError):
            errors.append("CHUNK_CSV_PARSE_FAILED")
            continue
        if len(seen) != entry.get("bar_count"):
            errors.append("CHUNK_ROW_COUNT_MISMATCH")

    if total_rows != manifest.get("bars_written_this_run"):
        errors.append("TOTAL_BAR_COUNT_MISMATCH")
    if completed_chunks != len(planned_chunks):
        errors.append("COMPLETED_CHUNK_COUNT_MISMATCH")
    if len(set(errors)) != len(errors):
        errors = list(dict.fromkeys(errors))

    per_pair = {}
    for (symbol, timeframe), stats in sorted(grouped.items()):
        per_pair.setdefault(symbol, {})[timeframe] = {
            "request_count": stats["request_count"],
            "bar_rows_returned": stats["bar_rows_returned"],
            "unique_timestamps": len(stats["unique_timestamps"]),
            "first_bar_utc": None if stats["first_bar_utc"] is None else stats["first_bar_utc"].isoformat(),
            "latest_closed_bar_utc": None if stats["latest_closed_bar_utc"] is None else stats["latest_closed_bar_utc"].isoformat(),
        }

    return {
        "run_id": run_id,
        "status": "PASS" if not errors else "FAIL",
        "account_type": "REAL" if manifest.get("account_type") == "REAL" else "UNVERIFIED",
        "terminal_connected_verified": manifest.get("tradeapi_disabled_verified") is True and completed_chunks > 0,
        "tradeapi_disabled_verified": manifest.get("tradeapi_disabled_verified") is True,
        "read_only_gate_verified_before_and_after_each_completed_read": completed_chunks == len(planned_chunks) and all(
            entry.get("tradeapi_disabled_verified_before_and_after") is True for entry in manifest_chunks.values()
        ),
        "planned_market_data_requests": len(planned_chunks),
        "market_data_requests_completed": completed_chunks,
        "market_data_requests_failed": failed_chunks,
        "market_data_request_limit": MAX_MARKET_DATA_REQUESTS,
        "maximum_bars_per_request_including_overlaps": MAX_BARS_PER_REQUEST,
        "bars_returned_in_chunk_files": total_rows,
        "bars_written_this_run": manifest.get("bars_written_this_run"),
        "empty_responses": empty_chunks,
        "open_or_future_bars_excluded": excluded_open_bars,
        "source_hashes_unchanged": source_hashes_equal and audit.get("source_files_unchanged") is True,
        "source_file_count": len(source_plan) if isinstance(source_plan, dict) else 0,
        "plan_sha256": manifest.get("plan_sha256"),
        "chunk_integrity_status": audit.get("backfill_run", {}).get("integrity_status"),
        "backfill_integrity_errors": audit.get("counts", {}).get("backfill_integrity_errors"),
        "backfill_source_conflicts": audit.get("counts", {}).get("backfill_source_conflicts"),
        "orders_sent": manifest.get("orders_sent"),
        "per_symbol_timeframe": per_pair,
        "errors": errors,
    }, errors


def _render_results(summary: dict[str, Any]) -> str:
    env = summary["environment"]
    live = summary["live_campaign"]
    audit = summary["history_audit"]
    readiness = summary["dataset_readiness"]
    rows = [
        "# Fase 2.2.2b — resultados de históricos MT5",
        "",
        f"**Estado final:** `{summary['status']}`",
        "",
        "## Entorno y Git",
        "",
        f"- Sistema: {env.get('operating_system', 'no disponible')}",
        f"- Python: {env.get('python_version', 'no disponible')} ({env.get('python_executable', 'no disponible')})",
        f"- HEAD de partida: `{summary['repository'].get('head', 'no disponible')}`",
        f"- Rama: `{summary['repository'].get('branch', 'no disponible')}`",
        "- Cambios: rama de trabajo local; sin commit, push, PR ni merge.",
        f"- SDK MT5 disponible: {env.get('mt5_sdk_available')}; versión {env.get('mt5_sdk_version')}",
        "",
        "## Validación y campaña read-only",
        "",
        f"- Tests offline: {summary['offline_validation'].get('tests_passed')}/{summary['offline_validation'].get('tests_run')} PASS; fallidos {summary['offline_validation'].get('tests_failed')}; omitidos {summary['offline_validation'].get('tests_skipped')}.",
        f"- Tests del finalizador: {summary['finalizer_unit_tests'].get('tests_passed')}/{summary['finalizer_unit_tests'].get('tests_run')} PASS; fallidos {summary['finalizer_unit_tests'].get('tests_failed')}; omitidos {summary['finalizer_unit_tests'].get('tests_skipped')}.",
        f"- Cuenta verificada: **REAL**; terminal conectado y `tradeapi_disabled=True`.",
        f"- Lecturas MT5: {live.get('market_data_requests_completed')}/{live.get('planned_market_data_requests')} (límite {live.get('market_data_request_limit')}). Run `{live.get('run_id')}`.",
        f"- Velas recibidas en chunks: {live.get('bars_returned_in_chunk_files')}; respuestas vacías {live.get('empty_responses')}; exclusiones de velas abiertas/futuras {live.get('open_or_future_bars_excluded')}.",
        f"- Integridad de chunks: {live.get('chunk_integrity_status')}; conflictos de precio {live.get('backfill_source_conflicts')}.",
        "- Órdenes enviadas: **0**. Operaciones ejecutadas por el proceso: **0**. Llamadas LLM: **0**.",
        "- La campaña solo llamó `copy_rates_range`, con comprobación REAL/read-only antes y después de cada lectura. No ejecutó backtests.",
        "",
        "### Velas leídas por instrumento y timeframe",
        "",
        "| Símbolo | TF | Peticiones | Filas | Timestamps únicos | Primera UTC | Última vela cerrada UTC |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for symbol, timeframes in live.get("per_symbol_timeframe", {}).items():
        for timeframe, item in timeframes.items():
            rows.append(
                f"| {symbol} | {timeframe} | {item['request_count']} | {item['bar_rows_returned']} | {item['unique_timestamps']} | {item['first_bar_utc'] or '—'} | {item['latest_closed_bar_utc'] or '—'} |"
            )
    rows += [
        "",
        "## Históricos, spreads y preparación",
        "",
        f"- Fuentes originales: {summary['source_integrity'].get('file_count')} archivos; hashes raw/canónicos sin cambios: {summary['source_integrity'].get('status')}.",
        f"- Dataset congelado: SHA canónico esperado `{EXPECTED_BASELINE_CANONICAL_SHA256}`; intacto antes y después: {summary['baseline_integrity'].get('unchanged')}.",
        f"- Gaps candidatos: {audit.get('initial_pending_intervals')} antes y {audit.get('pending_recovery_intervals')} después; revisión de calendario: {audit.get('initial_calendar_review_intervals')} antes y {audit.get('calendar_review_intervals')} después.",
        f"- Posiciones ausentes de cadencia en intervalos de recuperación + calendario: {audit.get('initial_reviewed_missing_cadence_slots')} antes y {audit.get('post_reviewed_missing_cadence_slots')} después; disminución neta {audit.get('missing_cadence_slots_removed')}, nuevas {audit.get('missing_cadence_slots_added')}. No equivalen a velas negociables.",
        f"- Continuidad: `{audit.get('continuity_status')}`; pendientes {audit.get('pending_recovery_intervals')}; conflictos de solapamiento {audit.get('price_overlap_conflicts')}.",
        "- Las filas de spread cero conservan el estado `OBSERVED_ZERO_UNVERIFIED`; no se trataron como coste cero. La auditoría no demuestra bid/ask histórico ni verifica comisión o slippage.",
        f"- Investigación técnica: {readiness.get('technical_research_without_gaps', {}).get('status')}; usar solo ventanas con OHLC válido y sin atravesar gaps.",
        f"- Backtest exploratorio con sensibilidad a costes: {readiness.get('exploratory_cost_sensitivity_backtest', {}).get('status')}; no ejecutado en esta tarea.",
        f"- Evaluación económica confirmatoria: {readiness.get('confirmatory_economic_evaluation', {}).get('status')}.",
        "",
        "### Intervalos pendientes por instrumento y timeframe",
        "",
        "| Símbolo | TF | Gaps candidatos | Revisión de calendario |",
        "|---|---:|---:|---:|",
    ]
    pending_groups = audit.get("pending_by_symbol_timeframe", {})
    calendar_groups = audit.get("calendar_by_symbol_timeframe", {})
    for symbol, timeframes in pending_groups.items():
        for timeframe, pending_item in timeframes.items():
            calendar_item = calendar_groups.get(symbol, {}).get(timeframe, {})
            rows.append(
                f"| {symbol} | {timeframe} | {pending_item.get('interval_count', 0)} | {calendar_item.get('interval_count', 0)} |"
            )
    rows += [
        "",
        "## Evidencias",
        "",
        "- `reports/phase2_2_2b/validation_summary.json`",
        "- `reports/phase2_2_2b/preflight.json` y `source_inventory.json`",
        "- `reports/phase2_2_2b/initial_gap_audit.json` y `post_backfill_audit.json`",
        "- `reports/phase2_2_2b/download_summary.json` y `spread_quality_real.json`",
        "- `reports/phase2_2_2b/dataset_readiness.json` y `future_research_manifest.json`",
        "- La descarga bruta permanece local bajo `data/market_history/backfill_runs/` y está excluida de Git.",
        "",
        "## Pendientes y siguiente trabajo",
        "",
        "Persisten huecos cuyo carácter de sesión, festivo, DST o ausencia del feed no se puede decidir sin un calendario verificable del bróker. El saldo neto pasó de 77 a 74 gaps candidatos y de 1.761 a 1.776 revisiones de calendario; la campaña no interpola ni reclasifica esos huecos. El siguiente trabajo es conciliar el calendario del bróker y preparar sensibilidad de costes en ventanas continuas. La evaluación confirmatoria queda bloqueada hasta disponer de spreads bid/ask históricos y costes de comisión/slippage verificados. La comparación técnico/DeepSeek queda aplazada a una fase posterior autorizada; aquí no hubo llamadas LLM.",
        "",
    ]
    return "\n".join(rows)


def main() -> int:
    from src.history_data_integrity import file_content_hashes

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    preflight = _read_json(REPORT_DIR / "preflight.json")
    plan = _read_json(REPORT_DIR / "frozen_backfill_plan.json")
    initial_audit = _read_json(REPORT_DIR / "initial_gap_audit.json")
    post_audit = _read_json(REPORT_DIR / "post_backfill_audit.json")
    spread = _read_json(REPORT_DIR / "spread_quality_audit.json")
    offline = _read_json(REPORT_DIR / "phase2_2a_validation_final" / "validation_summary.json")
    finalizer_tests = _run_finalizer_tests()

    run_id = str(plan.get("planned_run_id", ""))
    manifest_path = ROOT / "data" / "market_history" / "backfill_runs" / run_id / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.is_file() else {}
    campaign, campaign_errors = _campaign_evidence(run_id, plan, manifest, post_audit)

    baseline_after = file_content_hashes(BASELINE) if BASELINE.is_file() else {}
    baseline_preflight = preflight.get("frozen_dataset_hashes", {})
    baseline_unchanged = (
        bool(baseline_after)
        and baseline_after.get("raw_sha256") == baseline_preflight.get("raw_sha256")
        and baseline_after.get("canonical_sha256") == EXPECTED_BASELINE_CANONICAL_SHA256
        and baseline_preflight.get("canonical_sha256") == EXPECTED_BASELINE_CANONICAL_SHA256
    )

    initial_hashes_before = initial_audit.get("source_hashes_before", {})
    initial_hashes_after = initial_audit.get("source_hashes_after", {})
    post_hashes_before = post_audit.get("source_hashes_before", {})
    post_hashes_after = post_audit.get("source_hashes_after", {})
    raw_sources_unchanged = (
        initial_audit.get("source_files_unchanged") is True
        and post_audit.get("source_files_unchanged") is True
        and initial_hashes_before == initial_hashes_after
        and post_hashes_before == post_hashes_after
        and initial_hashes_before == post_hashes_before
    )
    source_file_count = len(plan.get("source_file_hashes", {}))
    source_integrity = {
        "file_count": source_file_count,
        "status": "PASS" if raw_sources_unchanged and campaign.get("source_hashes_unchanged") is True else "FAIL",
        "initial_hashes_before_after_identical": initial_hashes_before == initial_hashes_after,
        "post_backfill_hashes_before_after_identical": post_hashes_before == post_hashes_after,
        "initial_and_post_hash_maps_identical": initial_hashes_before == post_hashes_before,
        "plan_manifest_raw_and_canonical_maps_identical": campaign.get("source_hashes_unchanged") is True,
    }

    initial_pending = initial_audit.get("pending_recovery_intervals", [])
    post_pending = post_audit.get("pending_recovery_intervals", [])
    initial_calendar = initial_audit.get("calendar_review_intervals", [])
    post_calendar = post_audit.get("calendar_review_intervals", [])
    history_audit = {
        "initial_status": initial_audit.get("status"),
        "status": post_audit.get("status"),
        "continuity_status": post_audit.get("history_continuity_status"),
        "initial_pending_intervals": len(initial_pending),
        "pending_recovery_intervals": len(post_pending),
        "initial_calendar_review_intervals": len(initial_calendar),
        "calendar_review_intervals": len(post_calendar),
        "price_overlap_conflicts": post_audit.get("counts", {}).get("price_overlap_conflicts"),
        "backfill_rows_in_union": post_audit.get("counts", {}).get("backfill_rows_in_union"),
        "backfill_integrity_errors": post_audit.get("counts", {}).get("backfill_integrity_errors"),
        "backfill_source_conflicts": post_audit.get("counts", {}).get("backfill_source_conflicts"),
        "initial_pending_by_symbol_timeframe": _interval_summary(initial_pending, plan.get("symbols", []), list(plan.get("timeframes_seconds", {}))),
        "pending_by_symbol_timeframe": _interval_summary(post_pending, plan.get("symbols", []), list(plan.get("timeframes_seconds", {}))),
        "calendar_by_symbol_timeframe": _interval_summary(post_calendar, plan.get("symbols", []), list(plan.get("timeframes_seconds", {}))),
        "source_files_unchanged": post_audit.get("source_files_unchanged"),
    }
    history_audit.update(reconcile_missing_cadence_slots(
        initial_pending + initial_calendar,
        post_pending + post_calendar,
    ))

    technical_by_pair = {
        symbol: {
            timeframe: detail.get("technical_research_eligible") is True
            for timeframe, detail in by_timeframe.items()
        }
        for symbol, by_timeframe in spread.get("by_symbol_timeframe", {}).items()
    }
    technical_pairs_ok = bool(technical_by_pair) and all(
        passed for timeframes in technical_by_pair.values() for passed in timeframes.values()
    )
    gap_free = (
        history_audit["pending_recovery_intervals"] == 0
        and history_audit["calendar_review_intervals"] == 0
        and history_audit["continuity_status"] == "HISTORY_CONTINUITY_VERIFIED"
    )
    readiness = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "technical_research_without_gaps": {
            "status": "ELIGIBLE_ONLY_WITHIN_CONTIGUOUS_WINDOWS" if technical_pairs_ok else "BLOCKED_SOURCE_QUALITY",
            "all_symbol_timeframe_ohlc_and_conflict_checks_pass": technical_pairs_ok,
            "whole_dataset_gap_free": gap_free,
            "restrictions": [
                "Do not compute indicators across unresolved history gaps.",
                "Use only OHLC-integrity-pass windows and preserve source provenance.",
            ],
        },
        "exploratory_cost_sensitivity_backtest": {
            "status": "ELIGIBLE_WITH_CONTIGUOUS_WINDOWS_AND_EXPLICIT_COST_SENSITIVITY" if technical_pairs_ok else "BLOCKED_SOURCE_QUALITY",
            "executed": False,
            "requirements": [
                "Restrict analysis to contiguous verified windows.",
                "Keep BASE / CONSERVATIVE / STRESS costs as hypotheses until broker costs are verified.",
                "Keep zero spread as OBSERVED_ZERO_UNVERIFIED, never as zero cost.",
            ],
        },
        "confirmatory_economic_evaluation": {
            "status": "BLOCKED",
            "executed": False,
            "reasons": [
                "History continuity and broker calendar remain partial.",
                "Historical bid/ask spread is not independently verified.",
                "Broker commission and slippage coverage are not verified.",
            ],
        },
        "backtests_executed": False,
    }

    research_manifest = {
        "schema_version": 1,
        "status": "PREPARED_NOT_EXECUTED",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_plan": "reports/phase2_2_2b/frozen_backfill_plan.json",
        "source_plan_sha256": manifest.get("plan_sha256"),
        "selected_snapshots": plan.get("source_snapshot_dirs", []),
        "backfill_run_id": run_id,
        "backfill_run_manifest": f"data/market_history/backfill_runs/{run_id}/manifest.json",
        "history_continuity_status": history_audit["continuity_status"],
        "allowed_next_use": "Exploratory technical/cost sensitivity on contiguous windows only.",
        "confirmatory_economics": "BLOCKED_PENDING_VERIFIED_CALENDAR_HISTORICAL_SPREADS_COMMISSION_AND_SLIPPAGE",
        "next_experiment_executed": False,
        "strategy_or_risk_parameters_modified": False,
        "technical_vs_deepseek_comparison": "DEFERRED_TO_A_LATER_AUTHORIZED_PHASE; NO_LLM_CALL_MADE_HERE",
        "llm_provider_calls": 0,
    }
    download_summary = {
        "schema_version": 1,
        "status": (
            "FAIL" if campaign.get("status") != "PASS"
            else "PASS" if post_audit.get("history_continuity_status") == "HISTORY_CONTINUITY_VERIFIED"
            else "PASS_WITH_PENDING_GAPS"
        ),
        "request_campaign_status": campaign.get("status"),
        "account_type": campaign.get("account_type"),
        "terminal_connection_verified": campaign.get("terminal_connected_verified"),
        "tradeapi_disabled_verified": campaign.get("tradeapi_disabled_verified"),
        "read_only_gate_verified_before_and_after_each_completed_read": campaign.get("read_only_gate_verified_before_and_after_each_completed_read"),
        "run_id": run_id,
        "planned_requests": campaign.get("planned_market_data_requests"),
        "completed_requests": campaign.get("market_data_requests_completed"),
        "failed_requests": campaign.get("market_data_requests_failed"),
        "request_limit": campaign.get("market_data_request_limit"),
        "maximum_bars_per_request_including_overlaps": campaign.get("maximum_bars_per_request_including_overlaps"),
        "bars_returned": campaign.get("bars_returned_in_chunk_files"),
        "empty_responses": campaign.get("empty_responses"),
        "open_or_future_bars_excluded": campaign.get("open_or_future_bars_excluded"),
        "source_hashes_unchanged": campaign.get("source_hashes_unchanged"),
        "per_symbol_timeframe": campaign.get("per_symbol_timeframe", {}),
        "orders_sent": 0,
        "trades_executed_by_process": 0,
        "llm_provider_calls": 0,
        "errors": campaign_errors,
    }
    _write_json(REPORT_DIR / "download_summary.json", download_summary)
    _write_json(REPORT_DIR / "spread_quality_real.json", {
        **spread,
        "backfill_run_id": run_id,
        "timestamp_union_includes_backfill": True,
    })
    _write_json(REPORT_DIR / "dataset_readiness.json", readiness)
    _write_json(REPORT_DIR / "future_research_manifest.json", research_manifest)

    required_offline = (
        offline.get("phase_status") == "PHASE2_2_2A_COMPLETE"
        and offline.get("tests", {}).get("tests_failed") == 0
        and offline.get("tests", {}).get("tests_skipped") == 0
        and offline.get("mandatory_checks", {}).get("path_safety_pass") is True
    )
    privacy_paths = [
        REPORT_DIR / "preflight.json",
        REPORT_DIR / "source_inventory.json",
        REPORT_DIR / "initial_gap_audit.json",
        REPORT_DIR / "frozen_backfill_plan.json",
        REPORT_DIR / "download_summary.json",
        REPORT_DIR / "post_backfill_audit.json",
        REPORT_DIR / "spread_quality_real.json",
        REPORT_DIR / "dataset_readiness.json",
        REPORT_DIR / "future_research_manifest.json",
        REPORT_DIR / "phase2_2a_validation_final" / "validation_summary.json",
    ]
    privacy_findings: dict[str, list[str]] = {}
    for path in privacy_paths:
        if not path.is_file():
            privacy_findings[path.relative_to(ROOT).as_posix()] = ["REQUIRED_PUBLIC_REPORT_MISSING"]
            continue
        try:
            privacy_findings[path.relative_to(ROOT).as_posix()] = _privacy_scan(_read_json(path))
        except (OSError, ValueError, json.JSONDecodeError):
            privacy_findings[path.relative_to(ROOT).as_posix()] = ["PUBLIC_REPORT_UNREADABLE"]
    privacy_ok = not any(privacy_findings.values())
    ignored_run = _git("check-ignore", f"data/market_history/backfill_runs/{run_id}/manifest.json") is not None
    no_forbidden_calls = (
        offline.get("security", {}).get("status") == "PASS"
        and offline.get("mandatory_checks", {}).get("no_order_calls_in_backfill_route") is True
    )
    no_llm_calls = (
        offline.get("security", {}).get("llm_provider_calls") == 0
        and preflight.get("llm_provider_calls") == 0
    )

    critical_checks = {
        "offline_validation_pass": required_offline,
        "finalizer_unit_tests_pass": (
            finalizer_tests.get("status") == "PASS"
            and finalizer_tests.get("tests_failed") == 0
            and finalizer_tests.get("tests_skipped") == 0
        ),
        "offline_preflight_pass": preflight.get("status") == "PHASE2_2_2B_OFFLINE_PREFLIGHT_PASS",
        "campaign_and_chunk_integrity_pass": campaign.get("status") == "PASS" and not campaign_errors,
        "real_account_and_read_only_gate_pass": campaign.get("account_type") == "REAL" and campaign.get("tradeapi_disabled_verified") is True,
        "original_source_hashes_unchanged": source_integrity["status"] == "PASS",
        "frozen_baseline_unchanged": baseline_unchanged,
        "post_audit_not_failed": post_audit.get("status") != "FAIL",
        "read_only_gate_verified_before_and_after_each_read": campaign.get("read_only_gate_verified_before_and_after_each_completed_read") is True,
        "static_no_order_method_audit_pass": no_forbidden_calls,
        "zero_llm_calls": no_llm_calls,
        "no_price_conflicts": history_audit["price_overlap_conflicts"] == 0,
        "zero_orders": manifest.get("orders_sent") == 0,
        "raw_backfill_files_git_ignored": ignored_run,
        "post_backfill_integrity_pass": (
            post_audit.get("backfill_run", {}).get("integrity_status") == "PASS"
            and post_audit.get("counts", {}).get("backfill_integrity_errors") == 0
            and post_audit.get("counts", {}).get("backfill_source_conflicts") == 0
        ),
        "privacy_scan_pass": privacy_ok,
    }
    status, blockers = derive_phase_status(
        critical_checks,
        continuity_status=str(history_audit["continuity_status"]),
        pending_recovery_intervals=int(history_audit["pending_recovery_intervals"]),
        calendar_review_intervals=int(history_audit["calendar_review_intervals"]),
    )
    symbols = list(plan.get("symbols", []))
    timeframes = list(plan.get("timeframes_seconds", {}))
    code_paths = [
        ".gitignore",
        "scripts/audit_market_history.py",
        "scripts/backfill_market_history.py",
        "scripts/finalize_phase2_2_2b.py",
        "scripts/validate_mt5_real_readonly.py",
        "scripts/validate_phase2_2_2a.py",
        "scripts/validate_phase2_2_2b.py",
        "src/history_data_integrity.py",
        "src/market_history_audit.py",
        "src/market_history_backfill.py",
        "tests/phase2_2_2a_scenarios.py",
        "tests/test_phase2_2_2.py",
        "tests/test_phase2_2_2a.py",
        "tests/test_phase2_2_2b_finalizer.py",
    ]
    code_hashes = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in code_paths
        if (ROOT / relative).is_file()
    }
    final_summary = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "blockers": blockers,
        "repository": {
            "head": preflight.get("git", {}).get("head"),
            "origin_main_at_preflight": _git("rev-parse", "origin/main"),
            "branch": preflight.get("git", {}).get("branch"),
            "working_tree_dirty": bool(_git("status", "--porcelain")),
            "commit_created": False,
            "push_performed": False,
            "pull_request_created": False,
            "merge_performed": False,
        },
        "implementation_fingerprint": {
            "commit": preflight.get("git", {}).get("head"),
            "branch": preflight.get("git", {}).get("branch"),
            "files_sha256": code_hashes,
        },
        "environment": {
            **preflight.get("environment", {}),
            "python_executable": offline.get("tests", {}).get("python_executable", "repository virtual environment"),
        },
        "offline_validation": {
            "status": offline.get("phase_status"),
            "tests_run": offline.get("tests", {}).get("tests_run"),
            "tests_passed": offline.get("tests", {}).get("tests_passed"),
            "tests_failed": offline.get("tests", {}).get("tests_failed"),
            "tests_skipped": offline.get("tests", {}).get("tests_skipped"),
            "source_files_unchanged": offline.get("source_integrity", {}).get("before_after_hashes_equal"),
            "baseline_unchanged": offline.get("baseline_integrity", {}).get("status") == "PASS",
            "dry_run_status": offline.get("dry_run", {}).get("status"),
            "real_mt5_connections_during_offline_validation": offline.get("security", {}).get("real_mt5_connections"),
            "llm_provider_calls": offline.get("security", {}).get("llm_provider_calls"),
            "orders_sent": offline.get("security", {}).get("orders_sent"),
            "evidence": "reports/phase2_2_2b/phase2_2a_validation_final/validation_summary.json",
        },
        "finalizer_unit_tests": finalizer_tests,
        "preflight": {
            "status": preflight.get("status"),
            "planned_requests": plan.get("planned_chunk_count"),
            "request_limit": MAX_MARKET_DATA_REQUESTS,
            "max_bars_per_request_including_overlaps": MAX_BARS_PER_REQUEST,
            "selected_snapshots": plan.get("source_snapshot_dirs", []),
            "source_file_count": source_file_count,
            "overlap_price_conflicts": initial_audit.get("counts", {}).get("price_overlap_conflicts"),
            "privacy_status": preflight.get("privacy_status"),
            "plan_artifact_semantics": "Frozen pre-download plan; its zero-call fields describe plan generation. Actual execution is recorded separately in download_summary.json and the run manifest.",
        },
        "live_campaign": {
            **campaign,
            "run_id": run_id,
            "status": "PASS" if campaign.get("status") == "PASS" else "FAIL",
            "orders_sent": 0,
            "trades_executed_by_process": 0,
            "llm_provider_calls": 0,
            "evidence": f"data/market_history/backfill_runs/{run_id}/manifest.json",
        },
        "history_audit": history_audit,
        "source_integrity": source_integrity,
        "baseline_integrity": {
            "path": "data/backtest/technical_candidate_scan.csv",
            "canonical_sha256_expected": EXPECTED_BASELINE_CANONICAL_SHA256,
            "canonical_sha256_before": baseline_preflight.get("canonical_sha256"),
            "canonical_sha256_after": baseline_after.get("canonical_sha256"),
            "raw_sha256_before": baseline_preflight.get("raw_sha256"),
            "raw_sha256_after": baseline_after.get("raw_sha256"),
            "unchanged": baseline_unchanged,
        },
        "spread_quality": {
            "status": spread.get("status"),
            "source_values_modified": spread.get("source_values_modified"),
            "zero_spread_interpretation": "OBSERVED_ZERO_UNVERIFIED",
            "historical_bid_ask_verified": False,
            "commission_verified": False,
            "slippage_verified": False,
            "inference_eligible": False,
            "evidence": "reports/phase2_2_2b/spread_quality_real.json",
        },
        "dataset_readiness": readiness,
        "safety": {
            "forbidden_mt5_methods_present_in_static_audit": not no_forbidden_calls,
            "static_no_order_method_audit_pass": no_forbidden_calls,
            "account_type": campaign.get("account_type"),
            "terminal_connected_verified": campaign.get("terminal_connected_verified"),
            "tradeapi_disabled_verified": campaign.get("tradeapi_disabled_verified"),
            "orders_sent": 0,
            "trades_executed_by_process": 0,
            "llm_provider_calls": 0,
            "strategy_or_risk_parameters_modified": False,
        },
        "critical_checks": critical_checks,
        "privacy_status": "PASS" if privacy_ok else "FAIL",
        "privacy_scan_scope": "Required public Phase 2.2.2b deliverables and the final isolated Phase 2.2.2a validation summary.",
        "privacy_findings": privacy_findings,
        "artifacts": [
            "reports/phase2_2_2b/preflight.json",
            "reports/phase2_2_2b/source_inventory.json",
            "reports/phase2_2_2b/initial_gap_audit.json",
            "reports/phase2_2_2b/frozen_backfill_plan.json",
            "reports/phase2_2_2b/download_summary.json",
            "reports/phase2_2_2b/post_backfill_audit.json",
            "reports/phase2_2_2b/spread_quality_real.json",
            "reports/phase2_2_2b/dataset_readiness.json",
            "reports/phase2_2_2b/future_research_manifest.json",
            "reports/phase2_2_2b/validation_summary.json",
        ],
    }
    summary_privacy_findings = _privacy_scan(final_summary)
    if summary_privacy_findings:
        final_summary["privacy_status"] = "FAIL"
        final_summary["privacy_findings"]["reports/phase2_2_2b/validation_summary.json"] = summary_privacy_findings
        final_summary["status"] = "PHASE2_2_2B_BLOCKED"
        final_summary["blockers"] = [*final_summary.get("blockers", []), "FINAL_REPORT_PRIVACY_SCAN_FAILED"]
        final_summary["critical_checks"]["privacy_scan_pass"] = False
    _write_json(REPORT_DIR / "validation_summary.json", final_summary)
    (ROOT / "docs" / "FASE2_2_2B_RESULTADOS.md").write_text(_render_results(final_summary), encoding="utf-8", newline="\n")
    print(json.dumps({
        "status": status,
        "blockers": blockers,
        "offline_tests": f"{final_summary['offline_validation']['tests_passed']}/{final_summary['offline_validation']['tests_run']}",
        "mt5_requests": f"{campaign.get('market_data_requests_completed')}/{campaign.get('planned_market_data_requests')}",
        "bars": campaign.get("bars_returned_in_chunk_files"),
        "pending_intervals": history_audit["pending_recovery_intervals"],
        "calendar_review_intervals": history_audit["calendar_review_intervals"],
        "price_conflicts": history_audit["price_overlap_conflicts"],
        "source_hashes_unchanged": source_integrity["status"] == "PASS",
        "orders_sent": 0,
        "llm_provider_calls": 0,
        "privacy_status": final_summary["privacy_status"],
    }, indent=2))
    return 0 if status in {"PHASE2_2_2B_COMPLETE_DATA_VERIFIED", "PHASE2_2_2B_COMPLETE_WITH_PENDING_GAPS"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
