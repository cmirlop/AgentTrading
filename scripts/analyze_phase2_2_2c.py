"""Run the focused offline validation and Phase 2.2.2c analysis."""

from __future__ import annotations

import json
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.phase2_2_2c_analysis import generate_phase2_2_2c_reports  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline Phase 2.2.2c market-history and calendar audit.")
    parser.add_argument(
        "--run-dir",
        default="data/market_history/backfill_runs/phase222b-local-20261115",
        help="Existing ignored local run folder containing manifest.json and chunks/.",
    )
    args = parser.parse_args()

    def progress(stage: str) -> None:
        print(json.dumps({"progress": stage}), flush=True)

    result = generate_phase2_2_2c_reports(ROOT, run_tests=True, progress_callback=progress, run_dir=args.run_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PHASE2_2_2C_PARTIAL_CALENDAR_UNKNOWN" else 2


if __name__ == "__main__":
    raise SystemExit(main())
