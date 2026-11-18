#!/usr/bin/env python3
"""Rebuild a deterministic inventory and conservative classification of gaps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_gap_classification import (  # noqa: E402
    AS_OF_UTC,
    EXPECTED_GAP_COUNT,
    _sha256,
    build_classification_summary,
    build_episode_groups,
    classify_gap,
    annotate_episode_alignment,
    extract_gap_inventory,
    official_dst_transition_dates,
    source_registry,
)
from src.public_2023_history_audit import (  # noqa: E402
    EXPECTED_FROZEN_SCAN_SHA256,
    audit_public_2023_history,
    validate_public_2023_source_manifest,
)


OUTPUT_DIR = Path("reports/phase2_2_2i")
INTEGRITY_PATH = Path("reports/phase2_2_2h/public_2023_integrity.json")
MANIFEST_PATH = Path("reports/phase2_2_2h/public_2023_source_manifest.json")
FROZEN_SCAN_PATH = Path("data/backtest/technical_candidate_scan.csv")
TRUSTED_HOSTS_PATH = Path("config/broker_calendar_trusted_sources.json")
PROTECTED_PATHS = [
    INTEGRITY_PATH,
    MANIFEST_PATH,
    Path("reports/phase2_2_2h/validation_summary.json"),
    Path("reports/phase2_2_2g/validation_summary.json"),
    Path("docs/FASE2_2_2H_RESULTADOS.md"),
    Path("docs/FASE2_2_2H_HANDOFF_LOCAL.md"),
    FROZEN_SCAN_PATH,
    TRUSTED_HOSTS_PATH,
]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _hashes(root: Path, paths: list[Path]) -> dict[str, str]:
    result = {}
    for relpath in paths:
        absolute = root / relpath
        if not absolute.is_file():
            raise FileNotFoundError(f"PROTECTED_FILE_MISSING:{relpath.as_posix()}")
        result[relpath.as_posix()] = _sha256(absolute)
    return result


def _git_output(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def validate(root: Path, output_dir: Path) -> dict[str, Any]:
    integrity_bytes_before = (root / INTEGRITY_PATH).read_bytes()
    manifest_bytes_before = (root / MANIFEST_PATH).read_bytes()
    integrity = json.loads(integrity_bytes_before)
    manifest = json.loads(manifest_bytes_before)
    protected_before = _hashes(root, PROTECTED_PATHS)
    if integrity.get("as_of_utc") != AS_OF_UTC:
        raise ValueError("SNAPSHOT_AS_OF_MISMATCH")
    if integrity.get("discontinuity_count_observed") != EXPECTED_GAP_COUNT:
        raise ValueError("FROZEN_REPORT_GAP_COUNT_MISMATCH")

    fresh_audit = audit_public_2023_history(root, base_sha=integrity["base_sha"])
    if fresh_audit.get("source_integrity_status") != "SOURCE_INTEGRITY_PASS":
        raise ValueError("FRESH_SOURCE_AUDIT_FAILED")
    if fresh_audit.get("discontinuity_count_observed") != EXPECTED_GAP_COUNT:
        raise ValueError("FRESH_GAP_COUNT_MISMATCH")
    if fresh_audit.get("series") != integrity.get("series"):
        raise ValueError("FRESH_AUDIT_DIFFERS_FROM_FROZEN_REPORT")
    manifest_validation = validate_public_2023_source_manifest(root, manifest, integrity)
    if manifest_validation.get("status") != "PASS":
        raise ValueError("SOURCE_MANIFEST_VALIDATION_FAILED")

    public_source_manifest_sha = manifest_validation["source_manifest_sha256"]
    source_hashes = [
        {
            "path": item["path"],
            "raw_sha256": item["raw_sha256"],
            "canonical_sha256": item["canonical_sha256"],
            "row_count": item["row_count"],
        }
        for item in sorted(manifest["source_manifest"]["sources"], key=lambda item: item["path"])
    ]
    gaps = extract_gap_inventory(root, manifest)
    registry = source_registry(AS_OF_UTC, public_source_manifest_sha)
    ids = {entry["evidence_id"]: entry["evidence_id"] for entry in registry["sources"]}
    # Source map keeps IDs readable at the classifier boundary.
    source_ids = {
        "public_data": ids["PUBLIC_DATA_2023_PLUS"],
        "bis_fx_context": ids["BIS_FX_GLOBAL_CONTEXT"],
        "uk_holidays": ids["UK_GOV_HOLIDAYS"],
        "us_holidays": ids["US_OPM_HOLIDAYS"],
        "us_dst": ids["US_NIST_DST"],
        "eu_dst": ids["EU_SUMMERTIME_DATES"],
    }
    transition_dates = official_dst_transition_dates()
    inventory = [classify_gap(gap, source_ids=source_ids, dst_transition_dates=transition_dates) for gap in gaps]
    annotate_episode_alignment(inventory)
    episodes = build_episode_groups(inventory)
    summary = build_classification_summary(inventory)

    frozen_scan_sha = _sha256(root / FROZEN_SCAN_PATH)
    if frozen_scan_sha != EXPECTED_FROZEN_SCAN_SHA256:
        raise ValueError("FROZEN_CANDIDATE_SCAN_HASH_MISMATCH")
    protected_after = _hashes(root, PROTECTED_PATHS)
    if protected_before != protected_after:
        raise ValueError("PROTECTED_INPUTS_CHANGED_DURING_VALIDATION")

    integrity_recheck = {
        "snapshot_as_of_utc": AS_OF_UTC,
        "base_sha": integrity.get("base_sha"),
        "repository_base_commit_sha": _git_output(root, "merge-base", "HEAD", "origin/main"),
        "branch": os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or _git_output(root, "branch", "--show-current"),
        "source_audit_module": "src/public_2023_history_audit.py",
        "source_audit_schema_version": integrity.get("schema_version"),
        "stored_integrity_report_sha256": hashlib.sha256(integrity_bytes_before).hexdigest(),
        "stored_manifest_file_sha256": hashlib.sha256(manifest_bytes_before).hexdigest(),
        "source_manifest_sha256": public_source_manifest_sha,
        "source_file_hashes": source_hashes,
        "manifest_validation": manifest_validation,
        "fresh_audit_status": fresh_audit.get("source_integrity_status"),
        "fresh_audit_gap_count": fresh_audit.get("discontinuity_count_observed"),
        "fresh_audit_matches_frozen_series": True,
        "frozen_candidate_scan_sha256": frozen_scan_sha,
        "frozen_candidate_scan_hash_semantics": "raw bytes; equals the historical canonical SHA-256 because no line-ending conversion is present",
        "protected_input_hashes_before": protected_before,
        "protected_input_hashes_after": protected_after,
        "original_csvs_modified": False,
        "historical_reports_modified": False,
    }

    payload_hashes = {
        "gap_inventory_sha256": hashlib.sha256(json.dumps(inventory, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "episode_groups_sha256": hashlib.sha256(json.dumps(episodes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "classification_summary_sha256": hashlib.sha256(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    }
    validation = {
        "schema_version": 1,
        "status": "PASS",
        "classification_status": "OBSERVATIONAL_CLASSIFICATION_COMPLETE_CAUSES_PARTLY_UNRESOLVED",
        "snapshot_as_of_utc": AS_OF_UTC,
        "base_sha": integrity.get("base_sha"),
        "repository_base_commit_sha": integrity_recheck["repository_base_commit_sha"],
        "branch": integrity_recheck["branch"],
        "source_audit_module": integrity_recheck["source_audit_module"],
        "source_audit_schema_version": integrity_recheck["source_audit_schema_version"],
        "branch_expected": "codex/phase2-2-2i-gap-classification",
        "gap_count_expected": EXPECTED_GAP_COUNT,
        "gap_count_actual": len(inventory),
        "episode_count": len(episodes),
        "source_manifest_sha256": public_source_manifest_sha,
        "integrity_recheck_report": "reports/phase2_2_2i/integrity_recheck.json",
        "output_hashes": payload_hashes,
        "actions": {"mt5_calls": 0, "orders": 0, "llm_paid_calls": 0, "backtests": 0, "market_data_downloads": 0, "network_calls_during_validation": 0},
        "execution_invariants": {"MT5_CALLS": 0, "ORDERS": 0, "LLM_PAID_CALLS": 0, "BACKTESTS": 0, "MARKET_CSV_BYTES_CHANGED": 0, "FROZEN_SCAN_CHANGED": 0},
        "limitations": [
            "No broker legal entity or dated broker calendar is verified; no gap is classified as a verified broker closure.",
            "Public general-market and holiday sources provide context only.",
            "Observed weekend-shaped cadence does not prove the broker was closed or the feed complete.",
        ],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "gap_inventory.json", {
        "schema_version": 1,
        "classifier_version": "market-gap-classifier-v1",
        "snapshot_as_of_utc": AS_OF_UTC,
        "source_manifest_sha256": public_source_manifest_sha,
        "gap_count": len(inventory),
        "gaps": inventory,
    })
    _write_json(output_dir / "gap_episode_groups.json", {
        "schema_version": 1, "snapshot_as_of_utc": AS_OF_UTC,
        "group_count": len(episodes), "episodes": episodes,
    })
    _write_json(output_dir / "source_evidence_registry.json", registry)
    _write_json(output_dir / "classification_summary.json", summary)
    _write_json(output_dir / "integrity_recheck.json", integrity_recheck)
    _write_json(output_dir / "validation_summary.json", validation)
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: detected repo)")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="new report directory")
    args = parser.parse_args()
    try:
        result = validate(args.root.resolve(), (args.root / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir.resolve())
    except Exception as exc:  # validation must fail closed and retain the concise cause
        print(f"VALIDATION_FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": result["status"],
        "gap_count": result["gap_count_actual"],
        "episode_count": result["episode_count"],
        "output_dir": str(args.output_dir),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
