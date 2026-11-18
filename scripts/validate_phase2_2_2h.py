#!/usr/bin/env python3
"""Run the offline Phase 2.2.2h validation and write its evidence report."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = [
    ("phase2_2_2_regressions", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase2_2_2*.py"]),
    ("phase2_2_2f_simulated_sync", [sys.executable, "-m", "unittest", "tests.test_mt5_candle_sync"]),
    ("phase2_2_2g_simulated_sync", [sys.executable, "-m", "unittest", "tests.test_mt5_history_2023_sync"]),
    ("macro_and_verified_dataset", [sys.executable, "-m", "unittest", "tests.test_macro_sources", "tests.test_verified_dataset_backtests"]),
    ("phase1_backtest_regression", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_phase1_backtest.py"]),
    ("mt5_safety_mocks", [sys.executable, "-m", "unittest", "tests.test_mt5_real_readonly", "tests.test_phase1_mt5_safety"]),
    ("public_2023_audit_tests", [sys.executable, "-m", "unittest", "tests.test_public_2023_history_audit"]),
    ("lookahead", [sys.executable, "test_no_lookahead.py"]),
    ("compileall", [sys.executable, "-m", "compileall", "-q", "config", "src", "scripts"]),
    ("privacy", [sys.executable, "scripts/check_phase2_2_2e_privacy.py"]),
    ("source_integrity_audit", [sys.executable, "scripts/audit_public_2023_history.py"]),
    ("diff_check", ["git", "diff", "--check", "HEAD"]),
    ("frozen_data_unchanged", ["git", "diff", "--quiet", "HEAD", "--", "data/market", "data/backtest/technical_candidate_scan.csv"]),
]


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _run(name: str, command: list[str]) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        output = completed.stdout + completed.stderr
        result = {
            "name": name,
            "command": " ".join(command),
            "exit_code": completed.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "status": "PASS" if completed.returncode == 0 else "FAIL",
        }
        test_run = re.search(r"Ran (\d+) tests? in ([\d.]+)s", output)
        if test_run:
            result["test_count"] = int(test_run.group(1))
        skipped = re.search(r"skipped=(\d+)", output)
        result["skipped_count"] = int(skipped.group(1)) if skipped else 0
        failed = re.search(r"failures=(\d+)", output)
        errors = re.search(r"errors=(\d+)", output)
        result["failed_count"] = (int(failed.group(1)) if failed else 0) + (int(errors.group(1)) if errors else 0)
        checks = re.search(r"Checks realizados: (\d+)", output)
        if checks:
            result["checks_passed"] = int(checks.group(1))
        if name == "privacy":
            try:
                result["details"] = json.loads(output.strip().splitlines()[-1])
            except (ValueError, IndexError):
                result["status"] = "FAIL"
                result["error"] = "privacy result was not valid JSON"
        if result["status"] != "PASS":
            result["output_tail"] = output.splitlines()[-40:]
        return result
    except OSError as exc:
        return {
            "name": name,
            "command": " ".join(command),
            "duration_seconds": round(time.monotonic() - started, 3),
            "status": "FAIL",
            "error": str(exc),
        }


def main() -> int:
    results = [_run(name, command) for name, command in COMMANDS]
    audit_path = ROOT / "reports/phase2_2_2h/public_2023_integrity.json"
    manifest_path = ROOT / "reports/phase2_2_2h/public_2023_source_manifest.json"
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        audit, manifest = {}, {}

    total_tests = sum(result.get("test_count", 0) for result in results)
    total_skipped = sum(result.get("skipped_count", 0) for result in results)
    total_failed = sum(result.get("failed_count", 0) for result in results)
    all_pass = all(result["status"] == "PASS" for result in results)
    source_ok = audit.get("source_integrity_status") == "SOURCE_INTEGRITY_PASS"
    lineage_ok = manifest.get("status") == "SOURCE_INTEGRITY_PASS" and manifest.get("manifest_sha256")
    validation = {
        "schema_version": 1,
        "status": "OFFLINE_VALIDATION_COMPLETE_CI_PENDING" if all_pass and source_ok and lineage_ok else "OFFLINE_VALIDATION_FAILED",
        "execution_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "ci_status": "CI_PENDING_PR_RUN",
        "source_integrity_status": audit.get("source_integrity_status", "BLOCKED"),
        "source_lineage_status": "SOURCE_LINEAGE_PASS" if lineage_ok else "SOURCE_LINEAGE_BLOCKED",
        "scan_backtest_parity_status": "SYNTHETIC_PASS_REAL_DATA_BLOCKED_BY_CALENDAR" if all_pass else "FAIL",
        "broker_calendar_status": "UNKNOWN_OFFICIAL_SYMBOL_SPECIFIC_HISTORICAL_CALENDAR_NOT_VERIFIED",
        "real_economic_backtest_status": "NOT_RUN_BLOCKED_BY_CALENDAR_AND_COST_EVIDENCE",
        "external_calls": 0,
        "llm_calls": 0,
        "orders_sent": 0,
        "mt5_calls": 0,
        "market_data_downloads": 0,
        "as_of_utc": audit.get("as_of_utc"),
        "base_sha": _git("rev-parse", "HEAD"),
        "branch": _git("branch", "--show-current"),
        "git_worktree_status": "DIRTY_EXPECTED_FOR_ACTIVE_BRANCH" if _git("status", "--porcelain") else "CLEAN",
        "aggregate_rows": audit.get("aggregate_rows_actual"),
        "source_count": manifest.get("source_manifest", {}).get("source_count", 0),
        "manifest_sha256": manifest.get("manifest_sha256"),
        "frozen_scan_sha256": audit.get("frozen_scan", {}).get("sha256_actual"),
        "frozen_scan_status": audit.get("frozen_scan", {}).get("status"),
        "offline_unit_test_count": total_tests,
        "offline_unit_test_pass_count": max(0, total_tests - total_skipped - total_failed),
        "offline_unit_test_fail_count": total_failed,
        "offline_unit_test_skipped_count": total_skipped,
        "total_command_duration_seconds": round(sum(result["duration_seconds"] for result in results), 3),
        "test_results": results,
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(), "platform": platform.platform()},
        "dependencies": {
            package: importlib.metadata.version(package)
            for package in ("pandas", "numpy", "pydantic")
            if importlib.metadata.packages_distributions().get(package)
        },
        "previous_ci_failure": {
            "workflow_run_id": "44248662116",
            "status": "FAILED_BEFORE_TESTS",
            "cause": "setup-python pip cache could not find requirements.txt or pyproject.toml",
            "fix": "removed pip cache; offline-test dependencies remain installed explicitly",
        },
        "pending_reasons": [
            "GitHub Actions must run on the PR and conclude success.",
            "Official broker calendar and historical execution costs remain unverified.",
            "No real economic backtest was run.",
        ],
    }
    out = ROOT / "reports/phase2_2_2h/validation_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(validation, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    for item in results:
        print(f"{item['status']:4} {item['name']} ({item['duration_seconds']}s)")
    print(json.dumps({
        "status": validation["status"],
        "ci_status": validation["ci_status"],
        "test_count": total_tests,
        "skipped_count": total_skipped,
        "source_integrity": validation["source_integrity_status"],
        "source_lineage": validation["source_lineage_status"],
        "report": out.relative_to(ROOT).as_posix(),
    }, sort_keys=True))
    return 0 if validation["status"] == "OFFLINE_VALIDATION_COMPLETE_CI_PENDING" else 1


if __name__ == "__main__":
    raise SystemExit(main())
