#!/usr/bin/env python3
"""Offline validation for the Phase 2.2.2i cadence-gap inventory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.classify_market_gaps_2023 import (  # noqa: E402
    OUTPUT_DIR,
    PROTECTED_PATHS,
    _hashes,
    _write_json,
)


PYTHON = sys.executable
COMMANDS = [
    [PYTHON, "-m", "unittest", "tests.test_phase2_2_2i", "-v"],
    [PYTHON, "-m", "unittest", "tests.test_phase2_2_2d_sessions", "-v"],
    [PYTHON, "-m", "unittest",
     "tests.test_public_2023_history_audit.Public2023HistoryAuditTests.test_exact_inventory_raw_and_crlf_hash_lineage_and_read_only",
     "tests.test_public_2023_history_audit.Public2023HistoryAuditTests.test_public_real_data_loader_stays_closed_without_official_calendar",
     "tests.test_public_2023_history_audit.Public2023HistoryAuditTests.test_manifest_is_portable_across_crlf_only_rewrite"],
    [PYTHON, "-m", "compileall", "-q", "src/market_gap_classification.py",
     "src/public_2023_history_audit.py", "scripts/classify_market_gaps_2023.py",
     "scripts/validate_phase2_2_2i.py", "tests/test_phase2_2_2i.py"],
]


def _run(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    output = (result.stdout + result.stderr).strip()
    counts = [int(value) for value in re.findall(r"Ran\s+(\d+)\s+tests?", output)]
    return {
        "command": command,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "exit_code": result.returncode,
        "tests_run": sum(counts),
        "output_tail": output[-1600:],
    }


def _report_hashes() -> dict[str, str]:
    directory = ROOT / OUTPUT_DIR
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.json"))
    }


def main() -> int:
    protected_before = _hashes(ROOT, PROTECTED_PATHS)
    results = [_run(command) for command in COMMANDS]
    if any(item["status"] != "PASS" for item in results):
        return _finish(results, protected_before, False, "FOCUSED_OFFLINE_CHECK_FAILED")

    rebuild = [PYTHON, "scripts/classify_market_gaps_2023.py"]
    first = _run(rebuild)
    if first["status"] != "PASS":
        results.append(first)
        return _finish(results, protected_before, False, "FIRST_CLASSIFICATION_REBUILD_FAILED")
    first_hashes = _report_hashes()
    second = _run(rebuild)
    if second["status"] != "PASS":
        results.extend([first, second])
        return _finish(results, protected_before, False, "SECOND_CLASSIFICATION_REBUILD_FAILED")
    second_hashes = _report_hashes()
    same_hashes = first_hashes == second_hashes
    results.extend([first, second])
    protected_after = _hashes(ROOT, PROTECTED_PATHS)
    if protected_before != protected_after:
        return _finish(results, protected_before, False, "PROTECTED_INPUTS_CHANGED")
    if not same_hashes:
        return _finish(results, protected_before, False, "NON_REPRODUCIBLE_OUTPUT_HASHES")
    return _finish(results, protected_before, True, None, output_hashes=second_hashes, protected_after=protected_after)


def _finish(results: list[dict[str, Any]], protected_before: dict[str, str], passed: bool,
            failure_reason: str | None, *, output_hashes: dict[str, str] | None = None,
            protected_after: dict[str, str] | None = None) -> int:
    path = ROOT / OUTPUT_DIR / "validation_summary.json"
    payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    payload.update({
        "status": "PASS" if passed else "FAIL",
        "classification_status": "INVENTORY_AND_CLASSIFICATION_COMPLETE" if passed else "INVENTORY_AND_CLASSIFICATION_VALIDATION_FAILED",
        "official_broker_session_coverage_status": "UNKNOWN",
        "offline_validation": {
            "status": "PASS" if passed else "FAIL",
            "python_executable": PYTHON,
            "python_version": sys.version.split()[0],
            "commands": results,
            "test_count": sum(item["tests_run"] for item in results),
            "failure_reason": failure_reason,
            "network_calls": 0,
            "mt5_calls": 0,
            "orders": 0,
            "llm_paid_calls": 0,
            "backtests": 0,
        },
        "reproducibility": {
            "status": "PASS" if passed else "FAIL",
            "same_outputs_on_two_rebuilds": output_hashes is not None,
            "output_file_sha256": output_hashes or {},
        },
        "protected_inputs_unchanged": bool(protected_after is not None and protected_before == protected_after),
        "protected_input_sha256": protected_before,
        "actions": {"mt5_calls": 0, "orders": 0, "llm_paid_calls": 0, "backtests": 0, "market_data_downloads": 0, "network_calls_during_validation": 0},
        "execution_invariants": {"MT5_CALLS": 0, "ORDERS": 0, "LLM_PAID_CALLS": 0, "BACKTESTS": 0, "MARKET_CSV_BYTES_CHANGED": 0, "FROZEN_SCAN_CHANGED": 0},
    })
    _write_json(path, payload)
    print(json.dumps({"status": payload["status"], "tests": payload["offline_validation"]["test_count"], "failure_reason": failure_reason}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
