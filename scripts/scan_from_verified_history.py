"""Build a causal technical candidate scan from explicitly verified history."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.market_config import SYMBOLS  # noqa: E402
from src.verified_history_scan import (  # noqa: E402
    load_verified_history_frames,
    scan_symbol_from_verified_frames,
    write_private_scan,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", required=True, help="Versioned input inventory; never defaults to the frozen candidate scan.")
    parser.add_argument("--session-manifest", required=True, help="Dated official broker calendar manifest; fails closed if incomplete.")
    parser.add_argument("--evaluation-start", required=True, help="UTC ISO timestamp, inclusive.")
    parser.add_argument("--evaluation-end", required=True, help="UTC ISO timestamp, inclusive.")
    parser.add_argument("--as-of", help="UTC cut-off; defaults to current UTC.")
    parser.add_argument("--output", default="reports/private/phase2_2_2d/scan_from_local_history.csv")
    args = parser.parse_args(argv)

    def progress(stage: str) -> None:
        print(json.dumps({"progress": stage}), flush=True)

    try:
        source_manifest = json.loads((ROOT / args.source_manifest).read_text(encoding="utf-8"))
        session_manifest = json.loads((ROOT / args.session_manifest).read_text(encoding="utf-8"))
        as_of = args.as_of or datetime.now(timezone.utc).isoformat()
        progress("verifying_explicit_source_manifest_and_calendar")
        frames, audit = load_verified_history_frames(
            ROOT,
            source_manifest,
            session_manifest,
            evaluation_start_utc=args.evaluation_start,
            evaluation_end_utc=args.evaluation_end,
            as_of_utc=as_of,
        )
        rows = []
        lineage = []
        counts = {}
        for symbol in SYMBOLS:
            progress(f"scanning_{symbol}")
            frame, symbol_lineage, symbol_counts = scan_symbol_from_verified_frames(
                symbol,
                frames[symbol],
                session_manifest,
                evaluation_start_utc=args.evaluation_start,
                evaluation_end_utc=args.evaluation_end,
                source_lineage=audit["provenance"],
            )
            rows.append(frame)
            lineage.extend(symbol_lineage)
            counts[symbol] = symbol_counts
        import pandas as pd

        result = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        if result.empty:
            print(json.dumps({
                "status": "SCAN_BLOCKED_NO_VERIFIED_MTF_WINDOWS",
                "counts": counts,
                "source_rows_read": audit["source_rows_read"],
                "unique_bar_count_by_symbol_timeframe": audit["unique_bar_count_by_symbol_timeframe"],
                "spread_variant_timestamp_count": audit["spread_variant_timestamp_count"],
                "lineage_sha256": audit["lineage_sha256"],
                "orders_sent": 0,
                "llm_calls": 0,
            }, sort_keys=True))
            return 3
        csv_hash, lineage_hash = write_private_scan(ROOT, result, lineage, output_path=args.output)
        print(json.dumps({
            "status": "SCAN_FROM_VERIFIED_HISTORY_READY",
            "rows": len(result),
            "counts": counts,
            "source_rows_read": audit["source_rows_read"],
            "spread_variant_timestamp_count": audit["spread_variant_timestamp_count"],
            "scan_sha256": csv_hash,
            "lineage_sha256": lineage_hash,
            "orders_sent": 0,
            "llm_calls": 0,
        }, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        code = str(exc) if str(exc).isascii() and str(exc).replace("_", "").replace(":", "").isalnum() else type(exc).__name__.upper()
        print(json.dumps({"status": "SCAN_BLOCKED", "error_code": code, "orders_sent": 0, "llm_calls": 0}, sort_keys=True))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
