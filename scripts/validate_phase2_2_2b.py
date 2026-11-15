"""Prepare and verify the Phase 2.2.2b offline backfill plan without connecting to MT5."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REPORT_DIR = ROOT / "reports" / "phase2_2_2b"
BASELINE = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
BASELINE_EXPECTED_CANONICAL_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
MAX_MT5_REQUESTS = 150
MAX_BARS_PER_REQUEST = 1000


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    from src.history_data_integrity import safe_atomic_write_bytes

    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    safe_atomic_write_bytes(path, encoded, authorized_root=ROOT)


def _privacy_scan(value: Any) -> list[str]:
    prohibited = re.compile(r"(account_number|account_id|login|server|balance|equity|ticket|deal|api[_-]?key|token|password|credential)", re.I)
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if prohibited.search(str(key)):
                findings.append(f"SENSITIVE_FIELD_NAME:{key}")
            findings.extend(_privacy_scan(item))
    elif isinstance(value, list):
        for item in value:
            findings.extend(_privacy_scan(item))
    elif isinstance(value, str):
        if re.search(r"(?i)(?:[A-Z]:\\|\\\\[^\\]+\\[^\\]+)", value):
            findings.append("ABSOLUTE_WINDOWS_PATH")
    return findings


def _choose_run_id() -> str:
    base = "phase222b-local-20261115"
    run_root = ROOT / "data/market_history/backfill_runs"
    candidate = base
    suffix = 1
    while (run_root / candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


def _source_inventory(audit: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    from src.history_data_integrity import file_content_hashes

    files: list[dict[str, Any]] = []
    for symbol, by_timeframe in audit.get("symbols", {}).items():
        for timeframe, item in by_timeframe.items():
            sources = {"previous": item.get("previous_source", {})}
            sources.update(item.get("snapshot_sources", {}))
            for source_name, summary in sources.items():
                relative = summary.get("path")
                if not relative:
                    continue
                path = ROOT / relative
                hashes = file_content_hashes(path) if path.is_file() else {}
                files.append({
                    "role": "original_market_history",
                    "source": source_name,
                    "path": relative,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bytes": path.stat().st_size if path.is_file() else None,
                    "row_count": summary.get("row_count"),
                    "first_utc": summary.get("first_utc"),
                    "last_utc": summary.get("last_utc"),
                    "closed_bar_count": summary.get("closed_bar_count"),
                    "open_or_future_bar_count": summary.get("open_or_future_bar_count"),
                    "ohlc_integrity_status": summary.get("ohlc_integrity_status"),
                    "duplicate_identical_count": summary.get("duplicate_identical_count"),
                    "duplicate_conflict_count": summary.get("duplicate_conflict_count"),
                    "raw_sha256": hashes.get("raw_sha256"),
                    "canonical_sha256": hashes.get("canonical_sha256"),
                    "canonicalization_policy": hashes.get("canonicalization_policy"),
                })

    frozen_hashes = file_content_hashes(BASELINE) if BASELINE.is_file() else {}
    snapshot_manifests = []
    for selected in audit.get("selected_snapshots", []):
        relative = selected["directory"] + "/manifest.json"
        path = ROOT / relative
        hashes = file_content_hashes(path) if path.is_file() else {}
        snapshot_manifests.append({
            "path": relative,
            "manifest_sha256": selected.get("manifest_sha256"),
            "raw_sha256": hashes.get("raw_sha256"),
            "canonical_sha256": hashes.get("canonical_sha256"),
            "generated_utc": selected.get("manifest_generated_utc"),
            "expected_csv_count": selected.get("expected_csv_count"),
            "actual_csv_count": selected.get("actual_csv_count"),
            "missing": selected.get("missing", []),
            "extra": selected.get("extra", []),
            "unexpected_entries": selected.get("unexpected_entries", []),
        })

    return {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_selection": plan.get("source_snapshot_dirs", []),
        "files": sorted(files, key=lambda item: (item["path"], item["symbol"], item["timeframe"])),
        "snapshot_manifests": snapshot_manifests,
        "frozen_dataset": {
            "path": "data/backtest/technical_candidate_scan.csv",
            "bytes": BASELINE.stat().st_size if BASELINE.is_file() else None,
            **frozen_hashes,
            "expected_canonical_sha256": BASELINE_EXPECTED_CANONICAL_SHA256,
            "canonical_integrity_status": "PASS" if frozen_hashes.get("canonical_sha256") == BASELINE_EXPECTED_CANONICAL_SHA256 else "FAIL",
        },
        "source_file_count_in_plan": plan.get("source_file_count"),
        "source_hashes_in_plan": plan.get("source_file_hashes", {}),
        "pairwise_price_conflicts": audit.get("counts", {}).get("price_overlap_conflicts"),
        "original_sources_unchanged_during_audit": audit.get("source_files_unchanged"),
    }


def main() -> int:
    from src.history_data_integrity import safe_atomic_write_bytes
    from src.market_history_audit import audit_market_history
    from src.market_history_backfill import MAX_BARS_PER_REQUEST as CODE_BAR_LIMIT, MAX_PROVIDER_CALLS, plan_chunks

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    started_utc = datetime.now(timezone.utc)
    before_head = _git("rev-parse", "HEAD")
    before_branch = _git("branch", "--show-current")
    initial_status = _git("status", "--porcelain") or ""
    sdk_version = None
    try:
        sdk_version = importlib.metadata.version("MetaTrader5")
    except importlib.metadata.PackageNotFoundError:
        pass
    as_of = datetime.now(timezone.utc)
    audit, spreads, plan = audit_market_history(ROOT, as_of_utc=as_of)
    run_id = _choose_run_id()
    plan["source_audit"] = "reports/phase2_2_2b/initial_gap_audit.json"
    plan["planned_run_id"] = run_id
    plan["max_mt5_market_data_requests"] = MAX_MT5_REQUESTS
    plan["max_bars_per_request_including_overlap"] = MAX_BARS_PER_REQUEST
    chunks = plan_chunks(
        plan.get("backfill_intervals", []),
        symbols=plan["symbols"],
        timeframes=plan["timeframes_seconds"],
        max_bars_per_request=plan["chunk_policy"]["max_bars_per_request"],
        overlap_bars_each_side=plan["chunk_policy"]["overlap_bars_each_side"],
    )
    plan["planned_chunk_count"] = len(chunks)
    plan["request_budget_status"] = "PASS" if len(chunks) <= MAX_MT5_REQUESTS else "BLOCKED_OVER_150_REQUESTS"

    frozen = (ROOT / "data/backtest/technical_candidate_scan.csv").read_bytes()
    from src.history_data_integrity import content_hashes

    frozen_hashes = content_hashes(frozen)
    audit_ready = audit.get("status") != "FAIL" and audit.get("source_files_unchanged") is True
    source_inventory = _source_inventory(audit, plan)
    baseline_ready = source_inventory["frozen_dataset"]["canonical_integrity_status"] == "PASS"
    source_conflicts = audit.get("counts", {}).get("price_overlap_conflicts", 0)
    no_live_import = "MetaTrader5" not in sys.modules
    download_eligible = bool(
        plan.get("status") == "HISTORY_BACKFILL_PREPARED"
        and audit_ready
        and baseline_ready
        and source_conflicts == 0
        and len(chunks) <= MAX_MT5_REQUESTS
        and CODE_BAR_LIMIT == MAX_BARS_PER_REQUEST
        and no_live_import
    )

    plan_path = REPORT_DIR / "frozen_backfill_plan.json"
    _write_json(plan_path, plan)
    relative_plan = plan_path.relative_to(ROOT).as_posix()
    command = [sys.executable, "scripts/backfill_market_history.py", "--dry-run", "--audit-plan", relative_plan]
    dry = subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    try:
        dry_payload = json.loads(dry.stdout)
    except (TypeError, ValueError):
        dry_payload = {}
    dry_run_checks = {
        "process_succeeded": dry.returncode == 0,
        "status_is_dry_run": dry_payload.get("status") == "DRY_RUN",
        "mt5_not_imported": dry_payload.get("mt5_imported") is False,
        "zero_mt5_connections": dry_payload.get("mt5_connections") == 0,
        "zero_orders": dry_payload.get("orders_sent") == 0,
        "zero_provider_calls": dry_payload.get("provider_calls") == 0,
        "request_count_within_limit": dry_payload.get("chunk_count", MAX_MT5_REQUESTS + 1) <= MAX_MT5_REQUESTS,
        "bar_cap_with_overlap": dry_payload.get("max_bars_per_request") == MAX_BARS_PER_REQUEST,
        "plan_download_eligible": dry_payload.get("download_eligible") is True,
    }
    dry_run_status = "PASS" if all(dry_run_checks.values()) else "FAIL_OR_BLOCKED"
    status = "PHASE2_2_2B_OFFLINE_PREFLIGHT_PASS" if download_eligible and dry_run_status == "PASS" else "PHASE2_2_2B_BLOCKED"

    preflight = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "environment": {
            "operating_system": platform.platform(),
            "python_version": platform.python_version(),
            "mt5_sdk_available": sdk_version is not None,
            "mt5_sdk_version": sdk_version,
            "mt5_terminal_connection": "NOT_CHECKED_OFFLINE_PREFLIGHT",
            "mt5_account_type": "NOT_CHECKED_OFFLINE_PREFLIGHT",
            "tradeapi_disabled": "NOT_CHECKED_OFFLINE_PREFLIGHT",
        },
        "git": {
            "head": before_head,
            "branch": before_branch,
            "working_tree_dirty_at_preflight": bool(initial_status),
            "fetch_completed_before_preflight": before_head == _git("rev-parse", "origin/main"),
        },
        "source_inventory": "reports/phase2_2_2b/source_inventory.json",
        "initial_gap_audit": "reports/phase2_2_2b/initial_gap_audit.json",
        "frozen_plan": relative_plan,
        "planned_run_id": run_id,
        "live_mt5_connection_attempted": False,
        "llm_provider_calls": 0,
        "orders_sent": 0,
        "market_data_request_count": len(chunks),
        "market_data_request_limit": MAX_MT5_REQUESTS,
        "dry_run": {
            "status": dry_run_status,
            "checks": dry_run_checks,
            "chunk_count": dry_payload.get("chunk_count"),
            "provider_calls": dry_payload.get("provider_calls"),
            "stdout_json_valid": bool(dry_payload),
        },
        "preflight_checks": {
            "all_snapshots_included": len(plan.get("source_snapshot_dirs", [])) == len(audit.get("selected_snapshots", [])),
            "original_sources_unchanged": audit.get("source_files_unchanged") is True,
            "no_price_overlap_conflicts": source_conflicts == 0,
            "frozen_dataset_canonical_hash_matches": baseline_ready,
            "source_integrity_pass": audit_ready,
            "plan_within_request_budget": len(chunks) <= MAX_MT5_REQUESTS,
            "no_mt5_import_in_preflight_process": no_live_import,
        },
        "frozen_dataset_hashes": frozen_hashes,
        "started_utc": started_utc.isoformat(),
        "completed_utc": datetime.now(timezone.utc).isoformat(),
    }
    reports: dict[str, Any] = {
        "reports/phase2_2_2b/preflight.json": preflight,
        "reports/phase2_2_2b/source_inventory.json": source_inventory,
        "reports/phase2_2_2b/initial_gap_audit.json": audit,
        "reports/phase2_2_2b/spread_quality_real.json": spreads,
        "reports/phase2_2_2b/frozen_backfill_plan.json": plan,
    }
    privacy_findings = {name: _privacy_scan(value) for name, value in reports.items()}
    privacy_ok = not any(privacy_findings.values())
    _write_json(REPORT_DIR / "download_summary.json", {
        "schema_version": 1,
        "status": "NOT_EXECUTED",
        "reason": "Offline preflight command never connects to MT5 or reads broker account data.",
        "run_id": run_id,
        "mt5_requests": 0,
        "bars_downloaded": 0,
        "orders_sent": 0,
        "llm_provider_calls": 0,
    })
    _write_json(REPORT_DIR / "post_backfill_audit.json", {"status": "NOT_AUDITED", "reason": "No live backfill run has been executed."})
    _write_json(REPORT_DIR / "dataset_readiness.json", {"status": "NOT_ASSESSED", "reason": "Final readiness follows the post-backfill audit."})
    validation = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "preflight_status": preflight["status"],
        "dry_run_status": dry_run_status,
        "privacy_status": "PASS" if privacy_ok else "FAIL",
        "privacy_findings": privacy_findings,
        "required_offline_validation": "PENDING_RUN_VALIDATE_PHASE2_2_2A_AND_UNIT_TESTS",
        "real_mt5_campaign": "NOT_EXECUTED",
        "orders_sent": 0,
        "trades_executed": 0,
        "llm_provider_calls": 0,
    }
    reports["reports/phase2_2_2b/download_summary.json"] = json.loads((REPORT_DIR / "download_summary.json").read_text(encoding="utf-8"))
    reports["reports/phase2_2_2b/post_backfill_audit.json"] = {"status": "NOT_AUDITED", "reason": "No live backfill run has been executed."}
    reports["reports/phase2_2_2b/dataset_readiness.json"] = {"status": "NOT_ASSESSED", "reason": "Final readiness follows the post-backfill audit."}
    privacy_findings = {name: _privacy_scan(value) for name, value in reports.items()}
    validation["privacy_status"] = "PASS" if not any(privacy_findings.values()) else "FAIL"
    validation["privacy_findings"] = privacy_findings
    for report_name, payload in reports.items():
        _write_json(ROOT / report_name, payload)
    _write_json(REPORT_DIR / "validation_summary.json", validation)

    print(json.dumps({
        "status": status,
        "plan_status": plan.get("status"),
        "snapshot_count": len(plan.get("source_snapshot_dirs", [])),
        "source_file_count": plan.get("source_file_count"),
        "price_conflicts": source_conflicts,
        "pending_intervals": audit.get("counts", {}).get("pending_recovery_intervals"),
        "planned_requests": len(chunks),
        "maximum_requests": MAX_MT5_REQUESTS,
        "download_eligible": download_eligible and dry_run_status == "PASS",
        "mt5_imported": False,
        "mt5_connections": 0,
        "orders_sent": 0,
        "provider_calls": 0,
        "privacy_status": validation["privacy_status"],
    }, indent=2))
    return 0 if validation["privacy_status"] == "PASS" and status == "PHASE2_2_2B_OFFLINE_PREFLIGHT_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
