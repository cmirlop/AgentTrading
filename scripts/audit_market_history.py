"""Audit committed historical price and spread CSVs without connecting to MT5."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import os

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.history_data_integrity import safe_atomic_write_bytes, validate_run_id  # noqa: E402
from src.market_history_audit import audit_market_history, parse_utc_timestamp  # noqa: E402


def _write_json(path: Path, value: dict) -> None:
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    safe_atomic_write_bytes(path, text.encode("utf-8"), authorized_root=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-utc", help="Optional fixed UTC audit time for repeatable reports")
    parser.add_argument("--backfill-run", help="Audit exactly this run; other run IDs are never mixed")
    parser.add_argument("--output-dir", help="Optional output directory; defaults under reports/phase2_2_2a")
    args = parser.parse_args(argv)
    try:
        if args.backfill_run:
            validate_run_id(args.backfill_run)
        as_of = parse_utc_timestamp(args.as_of_utc) if args.as_of_utc else datetime.now(timezone.utc)
        gaps, spreads, backfill_plan = audit_market_history(ROOT, as_of_utc=as_of, backfill_run=args.backfill_run)
        if args.output_dir:
            output_dir = Path(args.output_dir)
            output_dir = output_dir if output_dir.is_absolute() else ROOT / output_dir
        elif args.backfill_run:
            output_dir = ROOT / "reports/phase2_2_2a/backfill_audits" / args.backfill_run
        else:
            output_dir = ROOT / "reports/phase2_2_2a/audits"
        continuity_name = "post_backfill_audit.json" if args.backfill_run else "history_gap_audit.json"
        _write_json(output_dir / continuity_name, gaps)
        _write_json(output_dir / "spread_quality_audit.json", spreads)
        _write_json(output_dir / "future_mt5_backfill_plan.json", backfill_plan)
        print(json.dumps({
            "status": gaps["status"],
            "history_continuity_status": gaps["history_continuity_status"],
            "backfill_run": args.backfill_run,
            "snapshot_files": gaps["snapshot_file_inventory"]["actual_csv_count"],
            "audited_source_rows": gaps["counts"]["all_source_rows_audited"],
            "pending_recovery_intervals": gaps["counts"]["pending_recovery_intervals"],
            "calendar_review_intervals": gaps["counts"]["calendar_review_intervals"],
            "price_overlap_conflicts": gaps["counts"]["price_overlap_conflicts"],
            "source_files_unchanged": gaps["source_files_unchanged"],
            "mt5_connections": 0,
            "report_dir": os.path.relpath(output_dir, ROOT).replace("\\", "/"),
        }, indent=2))
        return 0 if gaps["status"] != "FAIL" and gaps["source_files_unchanged"] else 2
    except Exception as exc:
        code = str(exc) if str(exc).startswith(("RUN_ID_", "BACKFILL_")) else type(exc).__name__.upper()
        print(json.dumps({"status": "FAIL", "error_code": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
