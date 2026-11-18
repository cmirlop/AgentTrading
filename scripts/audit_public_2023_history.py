#!/usr/bin/env python3
"""Read-only integrity and provenance audit for the tracked public 2023+ CSVs."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.public_2023_history_audit import (  # noqa: E402
    audit_public_2023_history,
    build_public_2023_source_manifest,
    validate_public_2023_source_manifest,
)


def _git_value(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="reports/phase2_2_2h", help="Output directory inside the repository")
    args = parser.parse_args()
    out_dir = (ROOT / args.output_dir).resolve()
    try:
        out_dir.relative_to(ROOT.resolve())
    except ValueError:
        parser.error("--output-dir must be inside the repository")

    head = _git_value("rev-parse", "HEAD")
    base_sha = head or "UNKNOWN_GIT_HEAD"
    audit = audit_public_2023_history(ROOT, base_sha=base_sha)
    manifest: dict
    lineage_validation: dict
    if audit["source_integrity_status"] == "SOURCE_INTEGRITY_PASS":
        try:
            manifest = build_public_2023_source_manifest(audit)
            lineage_validation = validate_public_2023_source_manifest(ROOT, manifest, audit)
            lineage_status = "SOURCE_LINEAGE_PASS"
        except (ValueError, OSError) as exc:
            manifest = {
                "schema_version": 1,
                "status": "SOURCE_INTEGRITY_BLOCKED",
                "dataset_kind": "PUBLIC_2023_PLUS_REPOSITORY_CAPTURE",
                "errors": [str(exc)],
            }
            lineage_validation = {"status": "BLOCKED", "error": str(exc)}
            lineage_status = "SOURCE_LINEAGE_BLOCKED"
    else:
        manifest = {
            "schema_version": 1,
            "status": "SOURCE_INTEGRITY_BLOCKED",
            "dataset_kind": "PUBLIC_2023_PLUS_REPOSITORY_CAPTURE",
            "errors": audit["errors"],
        }
        lineage_validation = {"status": "BLOCKED", "reason": "source integrity did not pass"}
        lineage_status = "SOURCE_LINEAGE_BLOCKED"

    # Re-run the deterministic read-only audit and compare its semantic report.
    repeat = audit_public_2023_history(ROOT, base_sha=base_sha)
    deterministic = audit == repeat
    validation = {
        "schema_version": 1,
        "status": "OFFLINE_AUDIT_PASS" if deterministic and lineage_status == "SOURCE_LINEAGE_PASS" else "OFFLINE_AUDIT_BLOCKED",
        "ci_status": "CI_PENDING",
        "source_integrity_status": audit["source_integrity_status"],
        "source_lineage_status": lineage_status,
        "scan_backtest_parity_status": "SYNTHETIC_VALIDATION_REQUIRED_REAL_DATA_BLOCKED_BY_CALENDAR",
        "broker_calendar_status": "UNKNOWN_OFFICIAL_SYMBOL_SPECIFIC_HISTORICAL_CALENDAR_NOT_VERIFIED",
        "real_economic_backtest_status": "NOT_RUN_BLOCKED_BY_CALENDAR_AND_COST_EVIDENCE",
        "offline_audit_reproducibility_status": "PASS" if deterministic else "FAIL",
        "lineage_validation": lineage_validation,
        "external_calls": 0,
        "llm_calls": 0,
        "orders_sent": 0,
        "mt5_calls": 0,
        "market_data_downloads": 0,
        "as_of_utc": audit.get("as_of_utc"),
        "base_sha": base_sha,
        "branch": _git_value("branch", "--show-current"),
        "git_worktree_status": "CLEAN" if not _git_value("status", "--porcelain") else "DIRTY_EXPECTED_FOR_ACTIVE_BRANCH",
        "source_audit_path": "reports/phase2_2_2h/public_2023_integrity.json",
        "source_manifest_path": "reports/phase2_2_2h/public_2023_source_manifest.json",
        "manifest_sha256": manifest.get("manifest_sha256"),
        "source_count": manifest.get("source_manifest", {}).get("source_count", 0),
        "aggregate_rows": audit.get("aggregate_rows_actual"),
        "frozen_scan_sha256": audit.get("frozen_scan", {}).get("sha256_actual"),
        "frozen_scan_status": audit.get("frozen_scan", {}).get("status"),
        "execution_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "commands_executed": ["python scripts/audit_public_2023_history.py"],
        "tests": [],
        "pending_reasons": [
            "GitHub Actions must run on the PR before CI can be reported green.",
            "Official broker calendar and historical execution costs remain unverified.",
            "Real economic backtest is intentionally not run.",
        ],
        "errors": audit.get("errors", []),
    }
    _write_json(out_dir / "public_2023_integrity.json", audit)
    _write_json(out_dir / "public_2023_source_manifest.json", manifest)
    _write_json(out_dir / "validation_summary.json", validation)
    print(json.dumps({
        "audit_status": audit["status"],
        "lineage_status": lineage_status,
        "reproducible": deterministic,
        "rows": audit.get("aggregate_rows_actual"),
        "reports": str(out_dir.relative_to(ROOT)),
    }, sort_keys=True))
    return 0 if validation["status"] == "OFFLINE_AUDIT_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
