"""Run focused, offline Phase 2.2.2a history-backfill validation.

This command reads frozen local CSVs and exercises the downloader only with
synthetic providers. It never imports/connects to MT5, contacts an LLM provider,
downloads external data, runs a backtest, or sends an order.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "phase2_2_2a"
BASELINE = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
BASELINE_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"
FIXED_AS_OF = datetime(2026, 11, 11, tzinfo=timezone.utc)
TEST_MODULES = ("test_phase2_2_2", "test_phase2_2_2a")


def _git(args: list[str]) -> str | None:
    result = subprocess.run(
        args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _run_test_module(module: str) -> dict[str, Any]:
    command = [sys.executable, "-m", "unittest", "-v", module]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT / "tests") + (os.pathsep + existing if existing else "")
    started = time.perf_counter()
    completed = subprocess.run(
        command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    output = completed.stdout
    ran_match = re.search(r"Ran (\d+) tests?", output)
    failed_match = re.search(r"FAILED \(([^)]*)\)", output)
    skipped_match = re.search(r"skipped=(\d+)", output)
    failures = 0
    errors = 0
    if failed_match:
        failures_match = re.search(r"failures=(\d+)", failed_match.group(1))
        errors_match = re.search(r"errors=(\d+)", failed_match.group(1))
        failures = int(failures_match.group(1)) if failures_match else 0
        errors = int(errors_match.group(1)) if errors_match else 0
    count = int(ran_match.group(1)) if ran_match else None
    skipped = int(skipped_match.group(1)) if skipped_match else 0
    passed = (count is not None and completed.returncode == 0)
    return {
        "module": module,
        "command": command,
        "status": "PASS" if passed else "FAIL",
        "exit_code": completed.returncode,
        "tests_run": count,
        "tests_passed": max(0, count - skipped - failures - errors) if count is not None else None,
        "tests_failed": failures + errors,
        "tests_skipped": skipped,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "output_tail": output[-6000:],
    }


def _security_source_check() -> dict[str, Any]:
    """Reject direct order-execution calls in this read-only backfill route."""
    import ast

    paths = [ROOT / "src/market_history_backfill.py", ROOT / "scripts/backfill_market_history.py"]
    findings: list[dict[str, str]] = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            findings.append({"path": str(path.relative_to(ROOT)), "reason": type(exc).__name__})
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else function.id if isinstance(function, ast.Name) else ""
            if name in {"order_send", "order_check", "symbol_select", "login"}:
                findings.append({"path": str(path.relative_to(ROOT)), "reason": f"FORBIDDEN_CALL_{name}"})
    return {
        "status": "PASS" if not findings else "FAIL",
        "files_scanned": [str(path.relative_to(ROOT)) for path in paths],
        "forbidden_calls": findings,
        "mt5_import_statement_present_for_explicit_download": "import MetaTrader5" in (ROOT / "scripts/backfill_market_history.py").read_text(encoding="utf-8"),
    }


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    tests_path = str(ROOT / "tests")
    if tests_path not in sys.path:
        sys.path.insert(0, tests_path)

    from phase2_2_2a_scenarios import run_end_to_end_scenarios, run_path_safety_scenario, run_portability_scenario
    from src.history_data_integrity import CANONICAL_HASH_POLICY
    from src.market_history_audit import audit_market_history
    from src.market_history_backfill import _source_hashes

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    start_utc = datetime.now(timezone.utc).isoformat()
    source_hashes_before = _source_hashes(ROOT)
    baseline_before = _sha256(BASELINE)

    audit, spreads, plan = audit_market_history(ROOT, as_of_utc=FIXED_AS_OF)
    e2e = run_end_to_end_scenarios()
    portability = run_portability_scenario()
    path_safety = run_path_safety_scenario()
    tests = [_run_test_module(module) for module in TEST_MODULES]

    # The plan is written only to the new phase's report area, then consumed
    # with --dry-run. Earlier Phase 2.2.2 artifacts remain untouched.
    audit_root = REPORT_DIR / "audits"
    _write_json(audit_root / "future_mt5_backfill_plan.json", plan)
    dry_command = [
        sys.executable, "scripts/backfill_market_history.py", "--dry-run",
        "--audit-plan", "reports/phase2_2_2a/audits/future_mt5_backfill_plan.json",
    ]
    dry_started = time.perf_counter()
    dry = subprocess.run(
        dry_command, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    try:
        dry_payload = json.loads(dry.stdout)
    except (TypeError, ValueError):
        dry_payload = {}
    dry_checks = {
        "process_succeeded": dry.returncode == 0,
        "dry_run_status": dry_payload.get("status") == "DRY_RUN",
        "mt5_not_imported": dry_payload.get("mt5_imported") is False,
        "zero_mt5_connections": dry_payload.get("mt5_connections") == 0,
        "zero_orders": dry_payload.get("orders_sent") == 0,
        "zero_provider_calls": dry_payload.get("provider_calls") == 0,
    }
    dry_report = {
        "command": dry_command,
        "status": "PASS" if all(dry_checks.values()) else "FAIL",
        "duration_seconds": round(time.perf_counter() - dry_started, 3),
        "checks": dry_checks,
        "chunk_count": dry_payload.get("chunk_count"),
        "mt5_imported": dry_payload.get("mt5_imported"),
        "mt5_connections": dry_payload.get("mt5_connections"),
        "orders_sent": dry_payload.get("orders_sent"),
        "provider_calls": dry_payload.get("provider_calls"),
        "output_tail": dry.stdout[-3000:],
    }

    source_hashes_after = _source_hashes(ROOT)
    baseline_after = _sha256(BASELINE)
    source_integrity_pass = source_hashes_before == source_hashes_after
    baseline_pass = baseline_before == baseline_after == BASELINE_SHA256
    continuity_audit = {
        "data_provenance": "FROZEN_LOCAL_HISTORICAL_FILES_NO_BACKFILL_RUN",
        "audit_status": audit.get("status"),
        "continuity_status": audit.get("history_continuity_status"),
        "snapshot_csv_count": audit.get("snapshot_file_inventory", {}).get("actual_csv_count"),
        "pending_recovery_intervals": audit.get("counts", {}).get("pending_recovery_intervals"),
        "calendar_review_intervals": audit.get("counts", {}).get("calendar_review_intervals"),
        "source_files_unchanged": audit.get("source_files_unchanged"),
        "critical_integrity_errors": audit.get("critical_integrity_errors", []),
        "backfill_run_included": audit.get("backfill_run") is not None,
        "real_recovery_performed": False,
        "source_hashes_before": audit.get("source_hashes_before", {}),
        "source_hashes_after": audit.get("source_hashes_after", {}),
    }
    _write_json(audit_root / "history_gap_audit.json", audit)
    _write_json(audit_root / "spread_quality_audit.json", spreads)
    _write_json(REPORT_DIR / "synthetic_end_to_end.json", e2e)
    _write_json(REPORT_DIR / "portability_test.json", portability)
    _write_json(REPORT_DIR / "path_safety_test.json", path_safety)
    _write_json(REPORT_DIR / "post_backfill_audit.json", e2e["post_backfill_audit"])

    security = _security_source_check()
    mandatory = {
        "frozen_source_audit_completed": audit.get("status") != "FAIL" and not audit.get("critical_integrity_errors"),
        "source_files_unchanged": source_integrity_pass and audit.get("source_files_unchanged") is True,
        "frozen_baseline_unchanged": baseline_pass,
        "focused_offline_tests_pass": all(item["status"] == "PASS" for item in tests),
        "synthetic_clean_recovery_verified": e2e["clean_recovery"]["continuity_status"] == "HISTORY_CONTINUITY_VERIFIED",
        "synthetic_conflict_blocked": e2e["conflict_recovery"]["continuity_status"] == "HISTORY_CONTINUITY_BLOCKED",
        "synthetic_incomplete_partial": e2e["empty_response_recovery"]["continuity_status"] == "HISTORY_CONTINUITY_PARTIAL",
        "portable_hashes_pass": all(portability.get(key) for key in (
            "source_raw_hashes_differ_after_crlf_conversion",
            "source_canonical_hashes_equal_after_crlf_conversion",
            "plan_accepted_on_crlf_sources",
            "auditor_accepts_canonical_chunk_hash",
        )),
        "path_safety_pass": (
            path_safety.get("all_invalid_run_ids_rejected") is True
            and path_safety.get("symlink_path_rejected") is True
            and path_safety.get("no_external_write") is True
        ),
        "synthetic_interruption_resume_pass": (
            e2e["interruption_and_resume"].get("interruption_observed") is True
            and e2e["interruption_and_resume"].get("first_completed_chunk_not_refetched") is True
        ),
        "synthetic_originals_unchanged": (
            e2e.get("original_sources_unchanged_after_all_scenarios") is True
            and e2e["clean_recovery"].get("originals_unchanged") is True
        ),
        "dry_run_without_mt5_pass": dry_report["status"] == "PASS",
        "no_order_calls_in_backfill_route": security["status"] == "PASS",
        "original_data_and_baseline_untouched": source_integrity_pass and baseline_pass,
    }
    status = "PHASE2_2_2A_COMPLETE" if all(mandatory.values()) else "PHASE2_2_2A_INCOMPLETE"
    zero_spread = {
        symbol: {
            timeframe: {
                "old_zero_percent": value.get("previous_source", {}).get("zero_percent"),
                "recent_zero_percent": value.get("recent_source", {}).get("zero_percent"),
                "union_zero_percent": value.get("combined_timestamp_union", {}).get("zero_percent"),
            }
            for timeframe, value in items.items()
        }
        for symbol, items in spreads.get("by_symbol_timeframe", {}).items()
    }
    test_summary = {
        "modules": tests,
        "tests_run": sum(item.get("tests_run") or 0 for item in tests),
        "tests_passed": sum(item.get("tests_passed") or 0 for item in tests),
        "tests_failed": sum(item.get("tests_failed") or 0 for item in tests),
        "tests_skipped": sum(item.get("tests_skipped") or 0 for item in tests),
        "duration_seconds": round(sum(item["duration_seconds"] for item in tests), 3),
        "python_executable": sys.executable,
        "python_version": sys.version,
    }
    report = {
        "schema_version": 1,
        "phase_status": status,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "validation_started_utc": start_utc,
        "repository": {
            "branch": _git(["git", "branch", "--show-current"]),
            "commit": _git(["git", "rev-parse", "HEAD"]),
            "base_commit": "2ffae736c8b5a561aa613d00d30dcef1584e671b",
            "working_tree_dirty": bool(_git(["git", "status", "--porcelain"])),
        },
        "runtime": {"python": sys.version, "platform": platform.platform(), "canonical_hash_policy": CANONICAL_HASH_POLICY},
        "audit": continuity_audit,
        "spread_quality": {
            "status": spreads.get("status"),
            "execution_inference_eligible": False,
            "zero_spread_percent_by_symbol_timeframe": zero_spread,
        },
        "baseline_integrity": {
            "path": str(BASELINE.relative_to(ROOT)).replace("\\", "/"),
            "expected_sha256": BASELINE_SHA256,
            "before_sha256": baseline_before,
            "after_sha256": baseline_after,
            "status": "PASS" if baseline_pass else "FAIL",
        },
        "source_integrity": {
            "file_count": len(source_hashes_before),
            "before_after_hashes_equal": source_integrity_pass,
            "hash_kind": "raw_sha256_and_canonical_sha256",
            "source_hashes_before": source_hashes_before,
            "source_hashes_after": source_hashes_after,
        },
        "tests": test_summary,
        "dry_run": dry_report,
        "synthetic_scenarios": {
            "provenance": e2e.get("data_provenance"),
            "mock_market_read_calls": e2e.get("mock_market_read_calls"),
            "clean_continuity": e2e.get("clean_recovery", {}).get("continuity_status"),
            "conflict_continuity": e2e.get("conflict_recovery", {}).get("continuity_status"),
            "empty_response_continuity": e2e.get("empty_response_recovery", {}).get("continuity_status"),
        },
        "security": {
            **security,
            "real_mt5_connections": 0,
            "real_market_downloads": 0,
            "llm_provider_calls": 0,
            "orders_sent": 0,
            "live_integrations": "NOT_RUN_BY_DESIGN",
        },
        "mandatory_checks": mandatory,
        "history_backfill_status": "HISTORY_BACKFILL_PREPARED",
        "history_backfill_completed": False,
        "commands_executed": [
            [sys.executable, "scripts/validate_phase2_2_2a.py"],
            *[item["command"] for item in tests],
            dry_command,
        ],
        "limitations": [
            "Post-backfill continuity evidence uses synthetic data and a fake MT5 provider; no real recovery was performed.",
            "A real later download and post-download audit still require the user's Windows MT5 REAL read-only terminal and explicit authorization.",
            "Unknown broker trading hours, holidays and feed omissions remain pending until verified against broker data/calendar.",
            "Manifest schema 1 is explicitly rejected; create a fresh schema 2 audit plan before downloading.",
        ],
    }
    _write_json(REPORT_DIR / "validation_summary.json", report)
    print(json.dumps({
        "phase_status": status,
        "continuity_audit_status": audit.get("history_continuity_status"),
        "tests_run": test_summary["tests_run"],
        "tests_passed": test_summary["tests_passed"],
        "tests_failed": test_summary["tests_failed"],
        "tests_skipped": test_summary["tests_skipped"],
        "source_files_unchanged": source_integrity_pass,
        "baseline_unchanged": baseline_pass,
        "real_mt5_connections": 0,
        "llm_provider_calls": 0,
        "orders_sent": 0,
        "report": "reports/phase2_2_2a/validation_summary.json",
    }, indent=2))
    return 0 if status == "PHASE2_2_2A_COMPLETE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
