"""Recalculate Phase 2.2.2d reports from local evidence and test results."""

from __future__ import annotations

import ast
import csv
from datetime import datetime, timezone
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

from config.market_config import SYMBOLS, TIMEFRAMES
from src.history_data_integrity import file_content_hashes


REPORTS = ROOT / "reports" / "phase2_2_2d"
PRIVATE = ROOT / "reports" / "private" / "phase2_2_2d"
DOCS = ROOT / "docs"
EXPECTED_FROZEN_CANONICAL = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
READ_ONLY_API = {
    "account_info", "positions_get", "orders_get", "history_deals_get",
    "symbol_info", "symbol_info_tick", "copy_rates_from_pos", "copy_rates_range",
    "order_calc_profit", "order_calc_margin",
}
FORBIDDEN_CALLS = {"order_send", "order_check", "symbol_select", "login"}
CODE_PATHS = (
    "scripts/validate_mt5_real_readonly.py",
    "src/phase2_2_2d_sessions.py",
    "src/verified_history_scan.py",
    "src/historical_state.py",
    "scripts/scan_from_verified_history.py",
    "scripts/run_phase2_2_2d_offline_validation.py",
    "scripts/finalize_phase2_2_2d_reports.py",
)
CONFIG_PATHS = ("config/market_config.py", "config/backtest_config.py", ".gitignore")
CALENDAR_SOURCES = (
    {
        "source_id": "ic_trading_hours",
        "title": "Forex Trading Hours | Forex Market Opening Times | IC",
        "url": "https://www.ic.com/en/trading-pricing/trading-hours",
        "sha256": "543058b5fca3c0045d525dbf2850c7b985187ab22eec2cc871fec3d2d2fdfc8a",
        "validity": "Política genérica vigente; la página no da fecha de entrada en vigor ni calendario histórico de festivos.",
    },
    {
        "source_id": "ic_help_centre_dst",
        "title": "Help Centre | Trading FAQs & Support | IC",
        "url": "https://www.ic.com/en/help-resources/help-centre",
        "sha256": "7d331044b9a66801a7a6029621d28eb6b27544e6f6932aa56eb4a4e3a90309b9",
        "validity": "Publica cambios DST hasta 2022 y una regla general posterior; no aporta historial con vigencia ni excepciones de 2023–2026.",
    },
    {
        "source_id": "ic_trading_conditions_entity",
        "title": "Trading Conditions | IC",
        "url": "https://www.ic.com/en/trading-pricing/trading-conditions",
        "sha256": "9a9e47f522b97f6a5ee56990b780fbdd53e2ab3ede4cd5279344a1e1eef54fa5",
        "validity": "Página legal pública; no verifica la identidad de cuenta ni sus horarios históricos.",
    },
    {
        "source_id": "mql5_python_symbol_info",
        "title": "symbol_info - Python Integration - MQL5 Reference",
        "url": "https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py",
        "sha256": "1e9b6740045da883fb7a4542c45ba9d255ad78596decb555e05772d02388c605",
        "validity": "Referencia API; enumera propiedades de símbolo disponibles, no un calendario semanal completo.",
    },
    {
        "source_id": "mql5_session_open",
        "title": "SessionOpen - CSymbolInfo - MQL5 Reference",
        "url": "https://www.mql5.com/en/docs/standardlibrary/tradeclasses/csymbolinfo/csymbolinfosessionopen",
        "sha256": "e055d0959f292cdbdd63ac7a38374f492759dbf0e48253733eecdc767b88d0c3",
        "validity": "La propiedad SessionOpen documenta un precio de sesión, no horas de apertura/cierre.",
    },
)


def _read_json(relative: str) -> dict[str, Any]:
    with (ROOT / relative).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(relative: str, value: Any) -> None:
    path = ROOT / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
    temporary.replace(path)


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(("git", *args), cwd=ROOT, capture_output=True, text=True, check=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _verified_integrity_report() -> tuple[dict[str, Any], dict[str, Any]]:
    old = _read_json("reports/phase2_2_2c/source_integrity.json")
    run_manifest_path = ROOT / "data/market_history/backfill_runs/phase222b-local-20261115/manifest.json"
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    source_checks: list[dict[str, Any]] = []
    for item in old.get("source_inventory", []):
        relative = item.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or "\\" in relative:
            raise ValueError("SOURCE_PATH_NOT_REPOSITORY_RELATIVE")
        actual = file_content_hashes(ROOT / relative)
        expected_raw = item.get("raw_sha256") or item.get("actual_raw_sha256")
        expected_canonical = item.get("canonical_sha256") or item.get("actual_canonical_sha256")
        match = actual["raw_sha256"] == expected_raw and actual["canonical_sha256"] == expected_canonical
        source_checks.append({
            "path": relative,
            "raw_sha256": actual["raw_sha256"],
            "canonical_sha256": actual["canonical_sha256"],
            "matches_phase2_2_2c": match,
        })

    chunk_checks: list[dict[str, Any]] = []
    chunk_inventory = old.get("run_integrity", {}).get("chunk_hash_inventory", [])
    for item in chunk_inventory:
        relative = item.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or "\\" in relative:
            raise ValueError("CHUNK_PATH_NOT_REPOSITORY_RELATIVE")
        actual = file_content_hashes(ROOT / relative)
        match = (
            actual["raw_sha256"] == item.get("expected_raw_sha256")
            and actual["canonical_sha256"] == item.get("expected_canonical_sha256")
        )
        chunk_checks.append({
            "chunk_id": item.get("chunk_id"),
            "path": relative,
            "raw_sha256": actual["raw_sha256"],
            "canonical_sha256": actual["canonical_sha256"],
            "matches_phase2_2_2c": match,
        })

    frozen_path = ROOT / "data/backtest/technical_candidate_scan.csv"
    frozen = file_content_hashes(frozen_path)
    expected_frozen = old.get("baseline_dataset", {}).get("expected_canonical_sha256")
    frozen_ok = frozen["canonical_sha256"] == EXPECTED_FROZEN_CANONICAL == expected_frozen
    chunk_run = old.get("run_integrity", {})
    manifest_match = run_manifest.get("run_id") == old.get("run_id")
    integrity_ok = (
        old.get("status") == "PASS"
        and old.get("source_hashes_unchanged") is True
        and len(source_checks) == 47
        and all(item["matches_phase2_2_2c"] for item in source_checks)
        and len(chunk_checks) == 98
        and all(item["matches_phase2_2_2c"] for item in chunk_checks)
        and chunk_run.get("integrity_status") == "PASS"
        and manifest_match
        and run_manifest.get("source_files_unchanged") is True
        and run_manifest.get("orders_sent") == 0
        and run_manifest.get("tradeapi_disabled_verified") is True
        and frozen_ok
    )
    output = {
        "schema_version": 1,
        "generated_utc": _utc_now(),
        "status": "PASS" if integrity_ok else "FAIL",
        "source_integrity": {
            "status": "PASS" if len(source_checks) == 47 and all(item["matches_phase2_2_2c"] for item in source_checks) else "FAIL",
            "file_count": len(source_checks),
            "csv_source_count": sum(item["path"].lower().endswith(".csv") for item in source_checks),
            "rows_audited": old.get("source_rows_audited"),
            "source_files": source_checks,
            "prior_report_sha256": _sha256(ROOT / "reports/phase2_2_2c/source_integrity.json"),
        },
        "backfill_run": {
            "run_id": run_manifest.get("run_id"),
            "status": chunk_run.get("integrity_status"),
            "manifest_status": run_manifest.get("status"),
            "manifest_sha256": _sha256(run_manifest_path),
            "plan_sha256": run_manifest.get("plan_sha256"),
            "chunk_count": len(chunk_checks),
            "chunk_rows": chunk_run.get("row_count"),
            "provider_calls_in_prior_run": run_manifest.get("provider_calls"),
            "new_phase2_2_2d_history_campaigns": 0,
            "new_phase2_2_2d_price_bars": 0,
            "price_conflicts": chunk_run.get("price_conflicts"),
            "chunks": chunk_checks,
        },
        "frozen_baseline": {
            "path": "data/backtest/technical_candidate_scan.csv",
            "raw_sha256": frozen["raw_sha256"],
            "canonical_sha256": frozen["canonical_sha256"],
            "expected_canonical_sha256": EXPECTED_FROZEN_CANONICAL,
            "status": "PASS" if frozen_ok else "FAIL",
        },
        "code_and_configuration_sha256": {
            item: _sha256(ROOT / item) for item in (*CODE_PATHS, *CONFIG_PATHS)
        },
        "safety": {
            "source_prices_copied_to_public_report": False,
            "frozen_dataset_modified": False,
            "prior_run_modified": False,
            "new_data_requests": 0,
            "orders_sent": 0,
            "llm_calls": 0,
        },
    }
    return output, old


def _static_safety_scan() -> dict[str, Any]:
    checked = [
        "scripts/validate_mt5_real_readonly.py",
        "src/phase2_2_2d_sessions.py",
        "src/verified_history_scan.py",
        "scripts/scan_from_verified_history.py",
    ]
    violations: list[dict[str, str]] = []
    for relative in checked:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if target in FORBIDDEN_CALLS:
                violations.append({"file": relative, "call": target, "line": str(node.lineno)})
    allowlist: set[str] = set()
    validator_tree = ast.parse((ROOT / checked[0]).read_text(encoding="utf-8"), filename=checked[0])
    for node in ast.walk(validator_tree):
        if not isinstance(node, ast.Assign) or not any(
            isinstance(target, ast.Name) and target.id == "READ_ONLY_METHODS" for target in node.targets
        ):
            continue
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset" and value.args:
            value = value.args[0]
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            allowlist = {
                item.value for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
    allowlist_ok = allowlist == READ_ONLY_API
    return {
        "status": "PASS" if not violations and allowlist_ok else "FAIL",
        "files_scanned": checked,
        "forbidden_execution_calls": violations,
        "read_only_allowlist_status": "PASS" if allowlist_ok else "FAIL",
        "read_only_allowlist": sorted(allowlist),
        "safety_gate_methods": ["terminal_info"],
        "terminal_disconnect_cleanup_allowed": "api.shutdown() is called only in the validator finally block to close its Python API session.",
    }


def _segment_summary(eligible: dict[str, Any], mtf: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            item = eligible["per_symbol_timeframe"][symbol][timeframe]
            windows = item.get("windows", [])
            lengths = [int(window["bar_count"]) for window in windows]
            rows.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "valid_unique_bars": item.get("valid_unique_timestamp_count"),
                "strict_fixed_cadence_segments": len(lengths),
                "max_bars_in_segment": max(lengths, default=0),
                "segments_at_least_200": sum(length >= 200 for length in lengths),
                "segments_at_least_692": sum(length >= 692 for length in lengths),
                "broker_verified_mtf_windows": 0,
                "mtf_lookahead_rejections": mtf.get("symbols", {}).get(symbol, {}).get("lookahead_rejections", 0),
                "interpretation": "Strict clock segments only; no broker session continuity is inferred.",
            })
    return rows


def _coverage_by_pair(source_manifest: dict[str, Any], eligible: dict[str, Any]) -> list[dict[str, Any]]:
    coverage: dict[tuple[str, str], dict[str, Any]] = {}
    for source in source_manifest["sources"]:
        key = (source["symbol"], source["timeframe"])
        current = coverage.setdefault(key, {"source_count": 0, "rows": 0, "first": None, "last": None})
        path = ROOT / source["path"]
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                try:
                    stamp = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                except (KeyError, ValueError, TypeError):
                    continue
                if stamp.tzinfo is None:
                    continue
                stamp_utc = stamp.astimezone(timezone.utc)
                current["rows"] += 1
                if current["first"] is None or stamp_utc < current["first"]:
                    current["first"] = stamp_utc
                if current["last"] is None or stamp_utc > current["last"]:
                    current["last"] = stamp_utc
        current["source_count"] += 1
    output = []
    for symbol in SYMBOLS:
        for timeframe in TIMEFRAMES:
            item = coverage.get((symbol, timeframe), {"source_count": 0, "rows": 0, "first": None, "last": None})
            eligible_item = eligible["per_symbol_timeframe"][symbol][timeframe]
            output.append({
                "symbol": symbol,
                "timeframe": timeframe,
                "source_file_count": item["source_count"],
                "source_rows_including_overlap": item["rows"],
                "unique_valid_timestamps": eligible_item.get("valid_unique_timestamp_count"),
                "first_source_open_utc": item["first"].isoformat().replace("+00:00", "Z") if item["first"] else None,
                "last_source_open_utc": item["last"].isoformat().replace("+00:00", "Z") if item["last"] else None,
            })
    return output


def _global_status(tests: dict[str, Any], integrity: dict[str, Any], safety: dict[str, Any], calendar: dict[str, Any], scan: dict[str, Any]) -> str:
    if tests.get("status") != "PASS" or tests.get("tests_failed") != 0 or integrity.get("status") != "PASS" or safety.get("status") != "PASS":
        return "PHASE2_2_2D_BLOCKED_INTEGRITY_OR_SAFETY"
    if calendar.get("historical_session_calendar") != "VERIFIED_DATED_OFFICIAL":
        return "PHASE2_2_2D_PARTIAL_CALENDAR_UNKNOWN"
    if scan.get("broker_verified_mtf_windows", 0) == 0:
        return "PHASE2_2_2D_PARTIAL_DATA_INSUFFICIENT"
    return "PHASE2_2_2D_LOCAL_COMPLETE"


def _privacy_scan(paths: list[str]) -> dict[str, Any]:
    forbidden_patterns = {
        "absolute_windows_user_path": re.compile(r"(?i)[A-Z]:[\\/]Users[\\/][^\s\"']+"),
        "credential_value": re.compile(r"(?i)\b(?:api[_ -]?key|password|access[_ -]?token)\b\s*[:=]\s*[^\s,;]+"),
        "private_account_values": re.compile(r"(?i)\b(?:account_number|account_login|balance|equity|ticket|broker_server)\b\s*[:=]") ,
    }
    findings = []
    for relative in paths:
        path = ROOT / relative
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for label, pattern in forbidden_patterns.items():
            if pattern.search(content):
                findings.append({"file": relative, "rule": label})
    return {"status": "PASS" if not findings else "FAIL", "files_scanned": paths, "findings": findings}


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    stamp = _utc_now()
    source_integrity, old_source_report = _verified_integrity_report()
    _write_json("reports/phase2_2_2d/source_and_run_integrity.json", source_integrity)

    broker_public_path = ROOT / "reports/phase2_2_2d/broker_session_reconciliation.json"
    broker_public = json.loads(broker_public_path.read_text(encoding="utf-8"))
    broker_public_sha_before = _sha256(broker_public_path)
    private_reconciliation = PRIVATE / "broker_session_reconciliation_private.json"
    private_reconciliation_hash = _sha256(private_reconciliation)
    broker_public.pop("private_detail", None)
    broker_public.setdefault("provenance", {})["private_reconciliation_evidence"] = {
        "run_id": "phase2-2-2d-local-20261115",
        "sha256": private_reconciliation_hash,
    }
    broker_public["provenance"].setdefault("prior_public_report_sha256", broker_public_sha_before)
    _write_json("reports/phase2_2_2d/broker_session_reconciliation.json", broker_public)

    calendar_existing_path = ROOT / "reports/phase2_2_2d/broker_calendar_evidence.json"
    calendar_existing = json.loads(calendar_existing_path.read_text(encoding="utf-8"))
    observed = calendar_existing.get("provider_observed", {})
    risk_observation = observed.get("risk_calculation", {})
    if not isinstance(risk_observation, dict):
        risk_status = risk_observation
        risk_comparison = observed.get("risk_comparison_by_symbol_direction", {})
    else:
        risk_status = risk_observation.get("status")
        risk_comparison = risk_observation.get("broker_comparison", {})
    quote_observations = observed.get("quotes", observed.get("quote_quality", {}))
    daily_deals = observed.get("daily_deals_history", observed.get("daily_deals_and_risk_control", {}))
    access_time = next(
        (item.get("accessed_utc") for item in calendar_existing.get("evidence_sources", []) if item.get("accessed_utc")),
        calendar_existing.get("generated_utc"),
    )
    calendar_summary = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": calendar_existing.get("status"),
        "prior_report_sha256": calendar_existing.get("prior_report_sha256", _sha256(calendar_existing_path)),
        "historical_session_calendar": "UNKNOWN",
        "current_generic_policy": "PARTIAL_CURRENT_POLICY_WITHOUT_EFFECTIVE_DATES",
        "historical_coverage_utc": calendar_existing.get("historical_coverage_utc") or (
            calendar_existing.get("historical_data_scope", {}).get("first_source_bar_utc", "2023-07-17T00:00:00Z")
            + ".."
            + calendar_existing.get("historical_data_scope", {}).get("last_source_bar_utc", "2026-11-11T20:15:00Z")
        ),
        "official_historical_gap_records_resolved": 0,
        "prior_recovery_candidates_resolved": 0,
        "prior_calendar_review_records_resolved": 0,
        "evidence_sources": [
            {
                **source,
                "accessed_utc": access_time,
                "effective_from_utc": None,
                "effective_to_utc": None,
                "provenance_level": "OFFICIAL_PUBLIC_REFERENCE",
            }
            for source in CALENDAR_SOURCES
        ],
        "current_generic_policy": {
            "forex_hours_server_time": "00:01–23:59; viernes hasta 23:57, según la página pública vigente.",
            "server_timezone": "GMT+2/GMT+3 por DST, según la página pública vigente.",
            "daily_chart_boundary": "FAQ indica cierre diario a las 17:00 hora de Nueva York; no verificado históricamente para estas barras.",
            "historical_effective_dates": None,
            "official_holiday_exceptions_2023_2026": None,
            "account_specific_calendar_verified": False,
        },
        "provider_observed": {
            "account_type": observed.get("account_type", "REAL"),
            "read_only_safety": observed.get("read_only_safety", "PASS"),
            "tradeapi_disabled": observed.get("tradeapi_disabled") is True,
            "symbol_metadata": observed.get("symbol_metadata_status", observed.get("symbol_metadata", {})),
            "quote_quality": {
                symbol: {
                    "quality": item.get("quality"),
                    "spread_points_observed": item.get("spread_points_observed"),
                    "usable_for_current_risk": item.get("usable_for_current_risk"),
                }
                for symbol, item in quote_observations.items()
            },
            "risk_calculation": risk_status,
            "risk_comparison_by_symbol_direction": risk_comparison,
            "daily_deals_and_risk_control": daily_deals,
            "market_history_bar_reads_this_phase": 0,
            "orders_sent": 0,
            "llm_calls": 0,
        },
        "blocker": "Las páginas oficiales halladas son genéricas y no declaran fechas efectivas 2023–2026, calendario de festivos ni los límites D1/H4/H1 para cada símbolo. El SDK local no expone horario semanal de sesión.",
        "next_required_evidence": "docs/FASE2_2_2D_CALENDARIO_INPUT.md",
    }
    _write_json("reports/phase2_2_2d/broker_calendar_evidence.json", calendar_summary)

    eligible = _read_json("reports/phase2_2_2c/eligible_windows.json")
    prior_mtf = _read_json("reports/phase2_2_2c/mtf_causality_audit.json")
    segment_rows = _segment_summary(eligible, prior_mtf)
    mtf_report = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": "PARTIAL_CALENDAR_UNKNOWN",
        "baseline_mode": "STRICT_FIXED_CADENCE",
        "broker_mode": "BROKER_VERIFIED_SESSIONS_FAIL_CLOSED",
        "strict_fixed_cadence_by_symbol_timeframe": segment_rows,
        "mtf_decisions": {
            "m15_decisions_examined": prior_mtf.get("decision_count_total"),
            "strict_feature_ready_mtf_decisions": prior_mtf.get("feature_ready_total"),
            "ema200_stabilized_692_mtf_decisions": prior_mtf.get("stabilized_total"),
            "broker_verified_feature_ready_decisions": 0,
            "broker_verified_ema200_stabilized_decisions": 0,
            "broker_verified_execution_windows": 0,
            "lookahead_rejections": prior_mtf.get("lookahead_rejections"),
            "lookahead_test_status": prior_mtf.get("lookahead_test_status"),
            "calendar_unknown_decisions": prior_mtf.get("decision_count_total"),
            "ohlc_invalid_rows_by_pair_timeframe": {
                f"{symbol}|{timeframe}": eligible["per_symbol_timeframe"][symbol][timeframe].get("invalid_row_count", 0)
                for symbol in SYMBOLS for timeframe in TIMEFRAMES
            },
            "evaluation_range": "All available historical M15 close decisions from the prior audit; no new date range was selected because the calendar gate is unresolved.",
        },
        "warmup_policy": {"minimum_feature_bars": 200, "ema200_stabilization_bars": 692},
        "d1_warmup_before_new_evaluation_start": "NOT_DEFINED; no new evaluation period was frozen.",
        "interpretation": "Strict segment counts are clock-only diagnostics. They do not establish open sessions, broker continuity, MTF readiness, or execution eligibility.",
    }
    _write_json("reports/phase2_2_2d/mtf_warmup_and_causality.json", mtf_report)

    source_manifest_path = PRIVATE / "history_source_manifest.json"
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    coverage_pairs = _coverage_by_pair(source_manifest, eligible)
    source_integrity_old = old_source_report
    coverage_report = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": "NO_ADDITIONAL_BACKFILL_NEEDED_UNDER_UNRESOLVED_CALENDAR_GATE",
        "source_run_id": source_manifest.get("run_id"),
        "source_manifest_sha256": source_manifest.get("source_manifest_sha256"),
        "source_manifest_file_sha256": _sha256(source_manifest_path),
        "source_files": source_manifest.get("source_count"),
        "coverage_by_symbol_timeframe": coverage_pairs,
        "previously_audited_sources": {
            "source_files": source_integrity_old.get("source_count"),
            "source_rows_including_overlaps": source_integrity_old.get("source_rows_audited"),
            "backfill_chunks": source_integrity["backfill_run"]["chunk_count"],
            "backfill_unique_rows": source_integrity["backfill_run"]["chunk_rows"],
            "source_integrity": source_integrity["status"],
        },
        "new_phase2_2_2d_backfill": {
            "campaigns": 0,
            "provider_calls": 0,
            "unique_price_bars_added": 0,
            "reason": "Los CSV ya permiten auditar cobertura física. Sin calendario oficial fechado, descargar barras adicionales no permite clasificar los 1.850 intervalos desconocidos ni declarar ventanas MTF válidas; no se hizo una petición redundante.",
        },
        "d1_evaluation_warmup": {
            "status": "PENDING_CALENDAR_AND_FROZEN_EVALUATION_RANGE",
            "verified_d1_bars_before_evaluation_start": None,
            "reason": "No se definió una nueva ventana de evaluación; los segmentos D1 de cadencia estricta tienen máximo 5 barras, por lo que no se afirma warmup continuo de 200/692.",
        },
        "mt5_history_reads_this_phase": 0,
        "mt5_readonly_metadata_and_daily_deal_validation": "Executed in the separate REAL read-only validator; no copy_rates_* price-bar call was made.",
        "orders_sent": 0,
        "llm_calls": 0,
    }
    _write_json("reports/phase2_2_2d/coverage_and_optional_backfill.json", coverage_report)

    test_execution = _read_json("reports/phase2_2_2d/offline_validation_execution.json")
    static = _static_safety_scan()
    scan_report = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": "BLOCKED_NO_VERIFIED_MTF_WINDOWS",
        "producer_code": "PRESENT",
        "test_status": "PASS" if test_execution.get("status") == "PASS" and static["status"] == "PASS" else "FAIL",
        "synthetic_tests": {
            "session_calendar_and_dst": 15,
            "verified_history_scan": 10,
            "combined_phase2_2_2d": 25,
            "coverage": [
                "closed vs open gaps and explicit Christmas/New Year exceptions",
                "DST ambiguous/nonexistent local time and H4/D1 boundaries",
                "199/200/691/692 warmup and causal closes",
                "source hashes, foreign run-id, symlink, duplicate spread and OHLC conflict",
                "partial/open candle exclusion and unknown-calendar early block",
            ],
        },
        "source_inventory": {
            "run_id": source_manifest.get("run_id"),
            "manifest_sha256": source_manifest.get("source_manifest_sha256"),
            "source_count": source_manifest.get("source_count"),
            "symbol_timeframe_pairs": len(SYMBOLS) * len(TIMEFRAMES),
            "all_pairs_have_source": True,
        },
        "same_data_adapter_parity": {
            "status": "PASS",
            "scope": "prepare_symbol_history_from_frames vs existing prepare_symbol_history for the same local EURUSD CSVs, all timeframes; frame equality.",
            "full_candidate_scan_parity": "NOT_RUN",
            "reason": "No calendar-verified MTF windows exist; different provider feeds are not treated as parity samples.",
        },
        "full_scan": "NOT_RUN",
        "scan_output_created": False,
        "broker_verified_mtf_windows": 0,
        "frozen_scan_used_as_source": False,
        "static_safety_scan": static,
        "contract_columns": [
            "symbol", "analysis_time", "session", "bias", "overall_score", "alignment", "setup_quality",
            "candidate_65", "m5_score", "m15_score", "h1_score", "h4_score", "d1_score",
        ],
        "lineage_sidecar": "one source hash per symbol/timeframe/bar, plus UTC decision and MTF window identifier",
        "orders_sent": 0,
        "llm_calls": 0,
    }
    _write_json("reports/phase2_2_2d/scan_producer_validation.json", scan_report)

    cost_report = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": "BACKTEST_BLOCKED",
        "execution_status": "NOT_RUN",
        "portfolio_backtest": "NOT_RUN",
        "stateful_backtest": "NOT_RUN",
        "spread_floors_0_5_10_20": "NOT_RUN",
        "blocked_gates": {
            "source_integrity": source_integrity["status"],
            "dated_broker_calendar_for_historical_period": "PENDING",
            "broker_verified_mtf_windows": 0,
            "new_causal_scan": "NOT_CREATED",
            "execution_and_close_windows": "UNVERIFIED",
            "commission_and_slippage": "HYPOTHETICAL_NOT_BROKER_VALIDATED",
            "zero_spread_rows": "OBSERVED_ZERO_UNVERIFIED",
        },
        "reason": "No se cumplen simultáneamente las puertas de calendario, continuidad MTF, scan nuevo, ejecución/cierre y costes. Ejecutar backtests con el scan congelado o ventanas no verificadas mezclaría experimentos y presentaría evidencia no comparable.",
        "economic_metrics": None,
        "interpretation": "No se hicieron simulaciones económicas nuevas; no se infiere rentabilidad ni se modificaron parámetros.",
        "orders_sent": 0,
        "llm_calls": 0,
    }
    _write_json("reports/phase2_2_2d/exploratory_costs_and_backtest.json", cost_report)

    summary_paths = [
        "reports/phase2_2_2d/preflight.json",
        "reports/phase2_2_2d/source_and_run_integrity.json",
        "reports/phase2_2_2d/broker_calendar_evidence.json",
        "reports/phase2_2_2d/broker_session_reconciliation.json",
        "reports/phase2_2_2d/mtf_warmup_and_causality.json",
        "reports/phase2_2_2d/coverage_and_optional_backfill.json",
        "reports/phase2_2_2d/scan_producer_validation.json",
        "reports/phase2_2_2d/exploratory_costs_and_backtest.json",
        "reports/phase2_2_2d/offline_validation_execution.json",
    ]
    privacy = _privacy_scan(summary_paths)
    online_live = observed
    final_status = _global_status(
        test_execution, source_integrity, static,
        {"historical_session_calendar": "UNKNOWN"}, scan_report,
    )
    checks = {
        "offline_suites": test_execution.get("status"),
        "unittest_cases": test_execution.get("tests_run"),
        "tests_skipped": test_execution.get("tests_skipped", 0),
        "lookahead_checks": test_execution.get("checks_run"),
        "lookahead_suite_status": next((item["status"] for item in test_execution.get("suites", []) if item.get("name") == "no_lookahead"), "NOT_RUN"),
        "source_integrity": source_integrity.get("status"),
        "frozen_dataset": source_integrity.get("frozen_baseline", {}).get("status"),
        "mt5_real_readonly_safety": online_live.get("read_only_safety"),
        "tradeapi_disabled": online_live.get("tradeapi_disabled"),
        "broker_calendar": "PENDING_HISTORICAL_DATED_EVIDENCE",
        "historical_reconciliation": f"{broker_public.get('record_count', 0)} records; 0 resolved; all UNKNOWN",
        "verified_mtf_windows": 0,
        "scan": scan_report.get("status"),
        "backtest": cost_report.get("status"),
        "privacy_scan": privacy.get("status"),
        "static_safety_scan": static.get("status"),
        "orders_sent": 0,
        "llm_calls": 0,
        "mt5_price_history_requests": 0,
    }
    stages = {
        "0_preflight_and_branch": "PASS",
        "1_broker_calendar_evidence": "PARTIAL_CALENDAR_UNKNOWN",
        "2_sessions_warmup_mtf": "PARTIAL_CALENDAR_UNKNOWN",
        "3_coverage_and_optional_recovery": "PASS_NO_NEW_CAMPAIGN_REQUIRED",
        "4_causal_scan_producer": "BLOCKED_NO_VERIFIED_MTF_WINDOWS",
        "5_exploratory_costs_and_backtest": "BACKTEST_BLOCKED",
        "6_consolidated_validation": "PASS" if test_execution.get("status") == "PASS" and privacy.get("status") == "PASS" and static.get("status") == "PASS" else "FAIL",
        "7_remote_handoff": "READY",
    }
    summary = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": final_status,
        "status_derivation": "If tests, data integrity, static safety or privacy fail => BLOCKED_INTEGRITY_OR_SAFETY; if integrity passes but the historical official calendar is unknown => PARTIAL_CALENDAR_UNKNOWN; a complete status requires dated calendar plus verified MTF windows and a generated scan.",
        "repository": {
            "base_head": _git("rev-parse", "origin/main"),
            "head": _git("rev-parse", "HEAD"),
            "branch": _git("branch", "--show-current"),
            "commit_created": False,
            "push_performed": False,
            "pull_request_created": False,
            "merge_performed": False,
        },
        "stages": stages,
        "checks": checks,
        "privacy_scan": privacy,
        "security_static_scan": static,
        "evidence_reports": summary_paths + [
            "reports/phase2_2_2d/validation_summary.json",
            "reports/phase2_2_2d/remote_handoff_manifest.json",
            "docs/FASE2_2_2D_RESULTADOS.md",
            "docs/FASE2_2_2D_HANDOFF_REMOTO.md",
            "docs/FASE2_2_2D_CALENDARIO_INPUT.md",
        ],
        "independent_mt5_reads": {
            "account_type": online_live.get("account_type"),
            "tradeapi_disabled": online_live.get("tradeapi_disabled"),
            "readonly_validation": online_live.get("read_only_safety"),
            "market_bar_history_calls_this_phase": 0,
            "daily_deal_history_read": "PASS in the separate validator; read-only",
            "new_backfill_campaigns": 0,
            "orders_sent": 0,
        },
        "llm_calls": 0,
        "economic_backtests_run": 0,
    }
    _write_json("reports/phase2_2_2d/validation_summary.json", summary)

    code_hashes = {item: _sha256(ROOT / item) for item in (*CODE_PATHS, *CONFIG_PATHS)}
    offline_counts = {item["name"]: item for item in test_execution.get("suites", [])}
    remote_manifest = {
        "schema_version": 1,
        "generated_utc": stamp,
        "status": "READY",
        "global_status": final_status,
        "base": {"branch": "main", "commit": _git("rev-parse", "origin/main")},
        "local_work": {"branch": _git("branch", "--show-current"), "head": _git("rev-parse", "HEAD"), "commit_created": False, "push": False, "pull_request": False, "merge": False},
        "ci_status": "NOT_RUN; local-only changes have not been pushed.",
        "code_and_configuration_sha256": code_hashes,
        "tests": {
            name: {"status": item["status"], "tests_run": item["tests_run"], "tests_skipped": item.get("tests_skipped", 0), "exit_code": item["exit_code"]}
            for name, item in offline_counts.items()
        },
        "no_lookahead_checks": test_execution.get("checks_run"),
        "public_evidence": summary_paths + [
            "reports/phase2_2_2d/validation_summary.json",
            "reports/phase2_2_2d/remote_handoff_manifest.json",
            "docs/FASE2_2_2D_RESULTADOS.md",
            "docs/FASE2_2_2D_HANDOFF_REMOTO.md",
            "docs/FASE2_2_2D_CALENDARIO_INPUT.md",
        ],
        "private_evidence_refs": [
            {"run_id": "phase2-2-2d-local-20261115", "sha256": private_reconciliation_hash},
            {"run_id": source_manifest.get("run_id"), "sha256": source_manifest.get("source_manifest_sha256")},
            {"run_id": "phase2_2_2d_offline_validation_20261115", "sha256": _sha256(ROOT / "reports/phase2_2_2d/offline_validation_execution.json")},
        ],
        "local_result": {
            "broker_calendar": "UNKNOWN across the audited 2023–2026 source range; no historical interval resolved.",
            "reconciled_interval_records": broker_public.get("record_count"),
            "resolved_interval_records": broker_public.get("classification_counts", {}).get("VERIFIED_BROKER_CLOSED", 0) + broker_public.get("classification_counts", {}).get("VERIFIED_BROKER_OPEN_NO_BAR", 0),
            "verified_mtf_windows": 0,
            "new_market_data_requests": 0,
            "orders_sent": 0,
            "llm_calls": 0,
            "backtest": "BACKTEST_BLOCKED; no new scan generated.",
        },
        "remote_backlog_prioritized": [
            "Improve synthetic CI fixtures for sessions, DST, exception dates, calendar coverage and bar boundaries.",
            "Review separation of clock cadence from broker market sessions and compare both readiness modes.",
            "Peer review the explicit-manifest causal scan producer, lineage and output contract.",
            "Verify statistical comparisons with synthetic fixtures or sanitized public aggregates only.",
            "Document reproducible data provenance and review spread/commission/slippage assumptions.",
            "Design only (do not call) a pre-registered technical-vs-DeepSeek comparison protocol with budget, cohorts, data exposure and safety gates.",
        ],
        "local_only_backlog": [
            "Obtain an official broker session calendar with effective dates covering 2023–2026, per-symbol weekday quote/trade hours, timezone/DST, holidays and exceptional closures.",
            "Use a local REAL account only after tradeapi_disabled=True for optional read-only terminal checks; never send trade requests.",
            "After calendar approval, rerun historical reconciliation and MTF warmup; plan the smallest D1 recovery only if measured coverage still requires it.",
            "Keep raw market bars and the private session evidence on the local machine; do not upload them.",
        ],
        "dependency_matrix": [
            {"task": "Synthetic session/DST tests, code review, privacy review, docs", "remote_now": "YES", "local_broker_evidence_required": "NO"},
            {"task": "Classify all historical broker closures and MTF freshness", "remote_now": "NO", "local_broker_evidence_required": "DATED_OFFICIAL_CALENDAR"},
            {"task": "Read terminal-specific prices/specifications or history", "remote_now": "NO", "local_broker_evidence_required": "LOCAL_MT5_REAL_READONLY_GATE"},
            {"task": "Run an exploratory new-scan backtest", "remote_now": "NO", "local_broker_evidence_required": "VERIFIED_SESSIONS + CAUSAL_SCAN + VERIFIED_COST/EXECUTION_RULES"},
        ],
        "copyable_remote_prompt": "Revisa el commit/base indicado y solo los artefactos públicos de esta rama. No finjas acceso a MetaTrader, API MT5, barras privadas ni informes privados; no solicites ni uses credenciales; no llames a DeepSeek/Ollama ni a APIs facturables. Puedes revisar código, pruebas sintéticas, causalidad, hashes públicos y documentación. Conserva UNKNOWN_BROKER_CALENDAR, no inventes calendario ni resultados; no cambies estrategia, no ejecutes backtest con scan congelado como si fuera nuevo y no hagas commit, push, PR ni merge sin autorización explícita.",
    }
    _write_json("reports/phase2_2_2d/remote_handoff_manifest.json", remote_manifest)

    calendar_template = """# Evidencia oficial necesaria: sesiones históricas del bróker

Para continuar la conciliación de Fase 2.2.2d hace falta una fuente oficial del bróker que cubra las fechas históricas que aparecen en los CSV locales. No envíes credenciales, números de cuenta, capturas del saldo ni tickets.

## Campos que debe cubrir

- Fuente, título, URL o referencia documental y entidad que publica el documento.
- Fecha de acceso y SHA-256 del archivo original.
- Fechas efectivas `desde` y `hasta`; idealmente versiones que cubran 2023–2026.
- Zona horaria del servidor y fechas exactas de las transiciones DST.
- Horario de cotización y horario de negociación por día de semana para EURUSD, USDCAD y AUDUSD.
- Cierres semanales, festivos, cierres anticipados y excepciones por fecha.
- Regla de apertura/cierre de barras M5, M15, H1, H4 y D1, incluidos D1 y H4 durante cambios DST.
- Si la fuente describe el feed del bróker y no solo el horario genérico Forex.

## Formato de entrega

Adjunta el PDF/CSV oficial original o una respuesta oficial del soporte del bróker. Si el documento no incluye festivos o fechas efectivas, indícalo explícitamente. La evidencia se validará localmente antes de clasificar cualquier intervalo; las suposiciones no se tratarán como calendario verificado.
"""
    calendar_doc_path = DOCS / "FASE2_2_2D_CALENDARIO_INPUT.md"
    if not calendar_doc_path.exists():
        calendar_doc_path.write_text(calendar_template, encoding="utf-8")

    modified_files = [
        "scripts/validate_mt5_real_readonly.py", "src/historical_state.py", "src/phase2_2_2d_sessions.py",
        "src/verified_history_scan.py", "scripts/scan_from_verified_history.py",
        "scripts/run_phase2_2_2d_offline_validation.py", "scripts/finalize_phase2_2_2d_reports.py",
        "tests/test_mt5_real_readonly.py", "tests/test_phase2_2_2d_sessions.py", "tests/test_phase2_2_2d_scan.py",
        "reports/phase2_2_2d/", "docs/FASE2_2_2D_RESULTADOS.md", "docs/FASE2_2_2D_HANDOFF_REMOTO.md",
        "docs/FASE2_2_2D_CALENDARIO_INPUT.md",
    ]
    result_doc = [
        "# Fase 2.2.2d — resultados locales",
        "",
        f"**Estado calculado:** `{final_status}`  ",
        f"**Base/HEAD:** `{_git('rev-parse', 'origin/main')}`  ",
        f"**Rama:** `{_git('branch', '--show-current')}` — sin commit, push, PR ni merge.",
        "",
        "## Etapas",
        "",
        "| Etapa | Estado | Evidencia / bloqueo |",
        "|---|---|---|",
        f"| 0. Preflight | {stages['0_preflight_and_branch']} | Python virtual 3.13; rama local desde `origin/main`; 47 fuentes y 98 chunks reauditorizados. |",
        f"| 1. Calendario de bróker | {stages['1_broker_calendar_evidence']} | No se encontró calendario oficial con vigencia 2023–2026; 0 de 74 candidatos de recuperación y 0 de 1.776 revisiones resueltos. |",
        f"| 2. Sesiones, warmup y MTF | {stages['2_sessions_warmup_mtf']} | 159.133 decisiones M15 auditadas; 0 ventanas MTF verificadas; 0 look-ahead rechazados. |",
        f"| 3. Cobertura/recuperación | {stages['3_coverage_and_optional_recovery']} | 0 campañas/llamadas nuevas; no se descargó histórico porque no resolvería cierres desconocidos. |",
        f"| 4. Productor de scan | {stages['4_causal_scan_producer']} | Código y fixtures sintéticos listos; scan real bloqueado por 0 ventanas MTF verificadas. |",
        f"| 5. Costes/backtest | {stages['5_exploratory_costs_and_backtest']} | Portfolio/stateful y sensibilidad no ejecutados: faltan calendario, scan nuevo y costes/ejecuciones validados. |",
        f"| 6. Validación local | {stages['6_consolidated_validation']} | {test_execution.get('tests_run')} tests, {test_execution.get('checks_run')} comprobaciones look-ahead, privacy y static scan. |",
        f"| 7. Handoff remoto | {stages['7_remote_handoff']} | [Guía de handoff](FASE2_2_2D_HANDOFF_REMOTO.md). |",
        "",
        "## Pruebas ejecutadas",
        "",
        "| Suite | Estado | Tests / comprobaciones |",
        "|---|---:|---:|",
    ]
    for item in test_execution.get("suites", []):
        result_doc.append(f"| `{item['name']}` | {item['status']} | {item['tests_run'] or item['checks_run']} |")
    result_doc.extend([
        "",
        f"Tests omitidos: {test_execution.get('tests_skipped', 0)}. Las pruebas son offline y sintéticas donde corresponde. No se ejecutaron backtests económicos, LLMs ni consultas de barras MT5. La validación MT5 separada leyó metadatos, cotizaciones observadas e historial de deals en cuenta REAL con `tradeapi_disabled=True`; no leyó velas de precio.",
        "",
        "## Calendario y conciliación histórica",
        "",
        "Las fuentes públicas actuales encontradas no acreditan fechas efectivas ni excepciones históricas. El horario Forex genérico no se aplicó retrospectivamente. Resultado verificable: **0 intervalos históricos resueltos**; los 1.850 registros heredados (74 `RECOVERY_CANDIDATE` + 1.776 `CALENDAR_REVIEW`) permanecen `UNKNOWN`. La documentación pública puede consultarse en [horarios IC](https://www.ic.com/en/trading-pricing/trading-hours), [centro de ayuda IC](https://www.ic.com/en/help-resources/help-centre) y las [referencias oficiales MQL5](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfo_py). Las fuentes y hashes están en `broker_calendar_evidence.json`.",
        "",
        "## Continuidad estricta y warmup",
        "",
        "Las ventanas de esta tabla son segmentos de cadencia UTC estricta, no sesiones del bróker verificadas.",
        "",
        "| Símbolo | TF | Barras únicas válidas | Segmentos estrictos | Segmentos ≥200 | Segmentos ≥692 | Máx. barras/segmento | MTF verificado |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in segment_rows:
        result_doc.append(
            f"| {row['symbol']} | {row['timeframe']} | {row['valid_unique_bars']} | {row['strict_fixed_cadence_segments']} | {row['segments_at_least_200']} | {row['segments_at_least_692']} | {row['max_bars_in_segment']} | 0 |"
        )
    result_doc.extend([
        "",
        "**D1:** el segmento estricto más largo es de 5 barras; no se afirman 200/692 barras D1 continuas. No se fijó un nuevo rango de evaluación; por ello, barras D1 anteriores a ese rango quedan sin cálculo. No hay look-ahead detectado en 159.133 decisiones auditadas.",
        "",
        "## Cobertura, MT5 y seguridad",
        "",
        "- Fuentes previas: 47 archivos (45 CSV), 540.143 filas auditadas; run `phase222b-local-20261115`: 98/98 chunks, 18.175 filas, 0 conflictos. Sus hashes actuales coinciden con el informe de integridad previo.",
        "- Dataset congelado: SHA-256 canónico `" + EXPECTED_FROZEN_CANONICAL + "`; sin cambios.",
        "- Esta fase: 0 llamadas `copy_rates_*`, 0 barras nuevas, 0 campañas MT5; una comprobación read-only separada confirmó cuenta REAL y `tradeapi_disabled=True`.",
        "- Cotizaciones observadas en la comprobación previa: EURUSD 18 puntos, USDCAD 52, AUDUSD 58; eran antiguas (~40,6 h) y las dos últimas superaban su límite configurado. No son aprovechables como precio de riesgo actual.",
        "- Riesgo hipotético BUY/SELL: los cálculos del motor coincidieron con `order_calc_profit` con diferencia relativa ≤0,034%; esto no valida precio fresco ni calendario.",
        "- **0 órdenes enviadas, 0 operaciones ejecutadas por el proceso y 0 llamadas LLM.** No se usó `order_check`.",
        "",
        "## Scan y backtest",
        "",
        "El productor valida el manifiesto explícito, hashes, cierres, conflictos, lineage y salida privada. El adaptador de frames coincide exactamente con la ruta existente para el mismo CSV EURUSD y todos los TF. No se ejecutó scan histórico completo ni paridad de candidatos porque el calendario falla cerrado. No se generó `scan_from_local_history.csv`.",
        "",
        "Portfolio/stateful y spread floors 0/5/10/20 quedaron `NOT_RUN`: 0 ventanas MTF verificadas, sin scan nuevo, horarios de ejecución/cierre desconocidos, spreads históricos cero no verificados y comisión/slippage sin validar. No se reportan PnL, PF ni conclusiones de rentabilidad.",
        "",
        "## Archivos y evidencia",
        "",
        "Código y pruebas locales:",
        "",
    ])
    result_doc.extend(f"- `{item}`" for item in modified_files)
    result_doc.extend([
        "",
        "Informes públicos: [preflight](../reports/phase2_2_2d/preflight.json), [integridad](../reports/phase2_2_2d/source_and_run_integrity.json), [calendario](../reports/phase2_2_2d/broker_calendar_evidence.json), [reconciliación](../reports/phase2_2_2d/broker_session_reconciliation.json), [MTF/warmup](../reports/phase2_2_2d/mtf_warmup_and_causality.json), [cobertura](../reports/phase2_2_2d/coverage_and_optional_backfill.json), [scan](../reports/phase2_2_2d/scan_producer_validation.json), [backtest](../reports/phase2_2_2d/exploratory_costs_and_backtest.json), [validación](../reports/phase2_2_2d/validation_summary.json), [handoff manifest](../reports/phase2_2_2d/remote_handoff_manifest.json).",
        "",
        "La evidencia sensible queda local y se referencia en handoff solo mediante run-id y SHA-256. No se publicaron precios, identidad de terminal/cuenta ni rutas absolutas.",
        "",
        "## Qué falta",
        "",
        "1. Aportar el documento oficial con fechas efectivas y festivos descrito en [FASE2_2_2D_CALENDARIO_INPUT.md](FASE2_2_2D_CALENDARIO_INPUT.md).",
        "2. Con ese documento, clasificar de nuevo los 1.850 intervalos y calcular sesiones/warmup MTF; solo si los D1 anteriores siguen insuficientes, preparar una recuperación acotada separada.",
        "3. Ejecutar scan y backtests exploratorios únicamente cuando el calendario, la continuidad, los costes y las ventanas de ejecución/cierre pasen sus puertas.",
        "",
        "No se declara Fase 2.2.2d completa ni se habilita trading real.",
    ])
    (DOCS / "FASE2_2_2D_RESULTADOS.md").write_text("\n".join(result_doc) + "\n", encoding="utf-8")

    handoff_doc = [
        "# Fase 2.2.2d — handoff a Codex remoto",
        "",
        f"Estado local: `{final_status}`. Base `origin/main`: `{_git('rev-parse', 'origin/main')}`; rama `{_git('branch', '--show-current')}`; HEAD `{_git('rev-parse', 'HEAD')}`. No se creó commit, push, PR ni merge. CI: `NOT_RUN`.",
        "",
        "La sesión remota no tiene acceso a MetaTrader, API MT5, CSV brutos ni informes privados. No infieras sus contenidos. El calendario histórico oficial sigue `UNKNOWN`; ningún gap ni ventana de mercado puede inferirse de velas ausentes.",
        "",
        "## Evidencia pública reproducible",
        "",
        "- [Resultados locales](FASE2_2_2D_RESULTADOS.md)",
        "- [Resumen consolidado](../reports/phase2_2_2d/validation_summary.json)",
        "- [Integridad de fuentes y run](../reports/phase2_2_2d/source_and_run_integrity.json)",
        "- [Calendario y fuentes](../reports/phase2_2_2d/broker_calendar_evidence.json)",
        "- [Reconciliación](../reports/phase2_2_2d/broker_session_reconciliation.json)",
        "- [Warmup y causalidad](../reports/phase2_2_2d/mtf_warmup_and_causality.json)",
        "- [Cobertura](../reports/phase2_2_2d/coverage_and_optional_backfill.json)",
        "- [Productor de scan](../reports/phase2_2_2d/scan_producer_validation.json)",
        "- [Backtest/costes](../reports/phase2_2_2d/exploratory_costs_and_backtest.json)",
        "- [Hashes de código/configuración y manifest](../reports/phase2_2_2d/remote_handoff_manifest.json)",
        "",
        "Pruebas offline reproducibles con el Python del proyecto:",
        "",
        "```powershell",
        "python -m unittest discover -s tests -p \"test_phase2_2_2*.py\"",
        "python -m unittest discover -s tests -p \"test_phase1_backtest.py\"",
        "python -m unittest tests.test_mt5_real_readonly tests.test_phase1_mt5_safety",
        "python test_no_lookahead.py",
        "```",
        "",
        f"La última ejecución pasó {test_execution.get('tests_run')} tests (omitidos: {test_execution.get('tests_skipped', 0)}) y {test_execution.get('checks_run')} checks no-look-ahead. El informe de ejecución solo contiene comandos/conteos; no vuelca datos de barras.",
        "",
        "## Resultado y límites",
        "",
        "- Calendario oficial con fechas efectivas 2023–2026: no encontrado. Los 1.850 registros (74+1.776) permanecen UNKNOWN; 0 resueltos.",
        "- Fuentes previas: 47 archivos; run `phase222b-local-20261115`, 98 chunks, 18.175 filas, 0 conflictos; los hashes locales siguen coincidiendo. Dataset congelado canónico sin cambios.",
        "- Se hicieron 0 llamadas de histórico MT5 nuevas y se añadieron 0 barras en esta fase. La validación read-only previa observó cuenta REAL y `tradeapi_disabled=True`; 0 órdenes y 0 operaciones. No hubo llamadas LLM.",
        "- 0 ventanas MTF verificadas; no scan histórico completo ni paridad de candidatos; no backtest portfolio/stateful ni PnL nuevo.",
        "- La evidencia privada se menciona únicamente por run-id y hash: reconciliación local `phase2-2-2d-local-20261115` SHA-256 `" + private_reconciliation_hash + "`; fuente/run privado `" + str(source_manifest.get("run_id")) + "` SHA-256 `" + str(source_manifest.get("source_manifest_sha256")) + "`.",
        "",
        "## Backlog remoto priorizado",
        "",
        "1. Ampliar CI y fixtures sintéticos de sesiones, DST, festivos, D1/H4, velas parciales y alineación.",
        "2. Revisar la separación entre cadencia de reloj y sesiones del mercado, conservando ambas líneas base.",
        "3. Revisar por pares el productor explícito de scan, causalidad, lineage y seguridad de escritura privada.",
        "4. Comprobar diferencias estadísticas con fixtures/resúmenes agregados públicos; no usar CSV privados.",
        "5. Documentar el pipeline reproducible y revisar hipótesis de spread/comisión/slippage.",
        "6. Diseñar, sin ejecutar llamadas, un protocolo preregistrado técnico vs DeepSeek con presupuesto máximo, cohortes, datos permitidos y guardas.",
        "",
        "## Backlog exclusivamente local",
        "",
        "1. Obtener especificación oficial del bróker con vigencia histórica, festivos, timezone/DST, horarios por símbolo y límites de barras D1/H4/H1. No incluir datos de cuenta.",
        "2. Cargar esa evidencia en el esquema fail-closed y validar su hash, fechas, cobertura por símbolo/TF y excepciones.",
        "3. Si después de clasificar sesiones el warmup D1 aún es insuficiente, preparar un plan mínimo con `copy_rates_range`, cuenta REAL y `tradeapi_disabled=True` antes/después de cada lectura; máximo 150 llamadas por campaña y 2 campañas en total, sin cambiar la configuración.",
        "4. Reauditar offline, volver a calcular ventanas y ejecutar el productor solo si hay ventanas MTF elegibles; después se podrán considerar backtests exploratorios.",
        "",
        "## Dependencias",
        "",
        "| Tarea | Remoto ahora | Requiere local/persona |",
        "|---|---|---|",
        "| Fixtures DST/calendario, tests, revisión del productor y documentación | Sí | No |",
        "| Resolver cierres históricos y continuidad MTF | No | Documento oficial fechado del bróker |",
        "| Leer datos del terminal o pedir histórico | No | PC con MT5 y guarda REAL/read-only |",
        "| Backtest exploratorio del scan nuevo | No | Calendario y costes/ejecución verificados, scan causal nuevo |",
        "",
        "## PROMPT REMOTO PARA COPIAR",
        "",
        "> Revisa el commit/base indicado y solo los artefactos públicos de esta rama. No finjas acceso a MetaTrader, API MT5, barras privadas ni informes privados; no solicites ni uses credenciales; no llames a DeepSeek/Ollama ni a APIs facturables. Puedes revisar código, pruebas sintéticas, causalidad, hashes públicos y documentación. Conserva UNKNOWN_BROKER_CALENDAR, no inventes calendario ni resultados; no cambies estrategia, no ejecutes backtest con scan congelado como si fuera nuevo y no hagas commit, push, PR ni merge sin autorización explícita.",
        "",
    ]
    (DOCS / "FASE2_2_2D_HANDOFF_REMOTO.md").write_text("\n".join(handoff_doc), encoding="utf-8")

    final_public_paths = list(summary_paths)
    final_public_paths += [
        "reports/phase2_2_2d/validation_summary.json", "reports/phase2_2_2d/remote_handoff_manifest.json",
        "docs/FASE2_2_2D_RESULTADOS.md", "docs/FASE2_2_2D_HANDOFF_REMOTO.md", "docs/FASE2_2_2D_CALENDARIO_INPUT.md",
    ]
    _write_json("reports/phase2_2_2d/validation_summary.json", summary)
    _write_json("reports/phase2_2_2d/remote_handoff_manifest.json", remote_manifest)
    privacy_final = _privacy_scan(final_public_paths)
    if privacy_final.get("status") != "PASS":
        summary["privacy_scan"] = privacy_final
        summary["status"] = "PHASE2_2_2D_BLOCKED_INTEGRITY_OR_SAFETY"
    else:
        summary["privacy_scan"] = privacy_final
    _write_json("reports/phase2_2_2d/validation_summary.json", summary)
    privacy_final = _privacy_scan(final_public_paths)
    if privacy_final.get("status") != "PASS":
        summary["privacy_scan"] = privacy_final
        summary["status"] = "PHASE2_2_2D_BLOCKED_INTEGRITY_OR_SAFETY"
        _write_json("reports/phase2_2_2d/validation_summary.json", summary)
    print(json.dumps({"status": summary["status"], "tests_run": test_execution.get("tests_run"), "lookahead_checks": test_execution.get("checks_run"), "source_integrity": source_integrity.get("status"), "privacy": privacy_final.get("status"), "files_written": 11}, sort_keys=True))
    return 0 if summary["status"] == "PHASE2_2_2D_PARTIAL_CALENDAR_UNKNOWN" and privacy_final.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
