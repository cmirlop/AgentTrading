"""Audit committed historical price and spread CSVs without connecting to MT5."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_history_audit import audit_market_history, parse_utc_timestamp  # noqa: E402


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of-utc", help="Optional fixed UTC audit time for repeatable reports")
    parser.add_argument("--output-dir", default="reports/phase2_2_2")
    args = parser.parse_args(argv)
    try:
        as_of = parse_utc_timestamp(args.as_of_utc) if args.as_of_utc else datetime.now(timezone.utc)
        gaps, spreads, backfill_plan = audit_market_history(ROOT, as_of_utc=as_of)
        output_dir = ROOT / args.output_dir
        _write_json(output_dir / "history_gap_audit.json", gaps)
        _write_json(output_dir / "spread_quality_audit.json", spreads)
        _write_json(output_dir / "future_mt5_backfill_plan.json", backfill_plan)
        print(json.dumps({
            "status": gaps["status"],
            "snapshot_files": gaps["snapshot_file_inventory"]["actual_csv_count"],
            "audited_source_rows": gaps["counts"]["all_source_rows_audited"],
            "pending_recovery_intervals": gaps["counts"]["pending_recovery_intervals"],
            "calendar_review_intervals": gaps["counts"]["calendar_review_intervals"],
            "price_overlap_conflicts": gaps["counts"]["price_overlap_conflicts"],
            "source_files_unchanged": gaps["source_files_unchanged"],
            "mt5_connections": 0,
            "report_dir": str(output_dir.relative_to(ROOT)).replace("\\", "/"),
        }, indent=2))
        return 0 if gaps["status"] != "FAIL" and gaps["source_files_unchanged"] else 2
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_code": type(exc).__name__.upper()}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
