"""Run the bounded macro-source to NEWS-02 research pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news_local.fixtures import FIXTURE_NAMES
from src.news_local.research import (
    MAX_SOURCE_REQUESTS,
    NewsResearchError,
    run_research,
)


SYMBOLS = ("EURUSD", "USDCAD", "AUDUSD")
MODES = ("offline", "collect", "local")


def _parse_aware_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        raise argparse.ArgumentTypeError("--as-of-utc must be an ISO timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("--as-of-utc requires an explicit timezone")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="offline")
    parser.add_argument("--symbol", choices=SYMBOLS, default="EURUSD")
    parser.add_argument("--fixture", choices=FIXTURE_NAMES)
    parser.add_argument("--snapshot", help="Private normalized-source snapshot to reuse")
    parser.add_argument("--output", help="Private structured result path")
    parser.add_argument("--request-limit", type=int, default=MAX_SOURCE_REQUESTS)
    parser.add_argument("--as-of-utc", type=_parse_aware_datetime)
    args = parser.parse_args(argv)

    try:
        report = run_research(
            root=ROOT,
            mode=args.mode,
            symbol=args.symbol,
            fixture=args.fixture,
            snapshot=args.snapshot,
            output=args.output,
            request_limit=args.request_limit,
            as_of_utc=args.as_of_utc,
        )
    except (NewsResearchError, ValueError) as exc:
        code = exc.code if isinstance(exc, NewsResearchError) else "INPUT_INVALID"
        print(json.dumps({"status": "FAIL", "error_code": code}))
        return 1

    analysis = report["analysis"]
    safe_summary = {
        "status": report["status"],
        "mode": report["mode"],
        "symbol": report["symbol"],
        "analysis_status": analysis["status"],
        "provider": analysis["provider"],
        "model": analysis["model"],
        "schema_version": analysis["schema_version"],
        "source_fetch": report["source_fetch"],
        "eligible_event_count": report["eligible_event_count"],
        "excluded_event_count": report["excluded_event_count"],
        "driver_currencies": report["driver_currencies"],
        "conditional_pair_directions": report["conditional_pair_directions"],
        "snapshot_path": report["snapshot_path"],
        "snapshot_sha256": report["snapshot_sha256"],
        "snapshot_integrity": report["snapshot_integrity"],
        "snapshot_source_payloads": report["snapshot_source_payloads"],
        "output_path": report["output_path"],
        "execution": analysis["execution"],
    }
    print(json.dumps(safe_summary, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"PASS", "PARTIAL", "PENDING"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
