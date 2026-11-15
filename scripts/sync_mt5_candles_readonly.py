"""Read-only audit and synchronization of the configured MT5 candle series."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.mt5_candle_sync import (  # noqa: E402
    MAX_BATCH_CALLS,
    CandleSyncError,
    SafetyGateError,
    audit_provider,
    public_summary,
    sync_from_run,
    write_public_reports,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit-only", action="store_true", help="Read MT5 and archive verified pages without changing public CSVs")
    mode.add_argument("--resume", action="store_true", help="Continue a private, checkpointed read-only provider scan")
    mode.add_argument("--sync", action="store_true", help="Apply a previously audited private run to the 15 public candle CSVs")
    parser.add_argument("--run-id", help="Required for --resume and --sync; optional stable id for --audit-only")
    parser.add_argument("--max-provider-calls", type=int, default=MAX_BATCH_CALLS, help=f"Rates calls in this batch (1-{MAX_BATCH_CALLS}); total run cap is 1500")
    parser.add_argument("--page-size", type=int, default=1000, help="Bars requested per positional history page (maximum 1000)")
    parser.add_argument("--check-idempotency", action="store_true", help="On --sync, verify a second cached pass adds zero bars and changes zero CSV hashes")
    args = parser.parse_args(argv)

    if args.max_provider_calls < 1 or args.max_provider_calls > MAX_BATCH_CALLS:
        parser.error(f"--max-provider-calls must be between 1 and {MAX_BATCH_CALLS}")
    if args.page_size < 1 or args.page_size > 1000:
        parser.error("--page-size must be between 1 and 1000")
    if (args.resume or args.sync) and not args.run_id:
        parser.error("--run-id is required with --resume and --sync")
    if args.audit_only and args.check_idempotency:
        parser.error("--check-idempotency only applies with --sync")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.sync:
        try:
            result = sync_from_run(ROOT, run_id, idempotency_check=args.check_idempotency)
            summary = public_summary(ROOT, run_id, result)
            write_public_reports(ROOT, summary)
            print(json.dumps({
                "status": result["status"],
                "run_id": run_id,
                "total_bars_added": summary["total_bars_added"],
                "provider_bar_queries": result["provider_calls"],
                "idempotency_second_pass": result["idempotency_second_pass"],
                "orders_sent": 0,
                "trades_executed": 0,
                "report": "reports/phase2_2_2f/validation_summary.json",
            }, indent=2))
            return 0 if result["status"] == "COMPLETE_VS_MT5_OBSERVED" else 4
        except SafetyGateError as exc:
            print(json.dumps({"status": "BLOCKED_SAFETY", "error_code": str(exc), "csv_written": False}, indent=2))
            return 3
        except (CandleSyncError, OSError, ValueError, KeyError) as exc:
            print(json.dumps({"status": "FAILED_VALIDATION", "error_code": str(exc) if str(exc).isupper() else type(exc).__name__}, indent=2))
            return 2

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print(json.dumps({"status": "BLOCKED_SAFETY", "error_code": "METATRADER5_MODULE_UNAVAILABLE"}, indent=2))
        return 3

    def progress(event: dict) -> None:
        # Only symbol/timeframe, UTC bar timestamps, counters and safe codes are emitted.
        print(json.dumps({"progress": event}, ensure_ascii=True), flush=True)

    try:
        result = audit_provider(
            mt5,
            root=ROOT,
            run_id=run_id,
            max_provider_calls=args.max_provider_calls,
            page_size=args.page_size,
            progress=progress,
            resume=args.resume,
        )
        summary = public_summary(ROOT, run_id)
        write_public_reports(ROOT, summary)
        print(json.dumps({
            "status": result.get("status"),
            "run_id": run_id,
            "as_of_utc": result.get("as_of_utc"),
            "provider_bar_queries": result.get("provider_calls", 0),
            "batch_provider_query_limit": args.max_provider_calls,
            "total_provider_query_limit": result.get("provider_call_limit_total", 1500),
            "orders_sent": 0,
            "trades_executed": 0,
            "csv_written": False,
            "private_run": f"reports/private/phase2_2_2f/{run_id}",
            "report": "reports/phase2_2_2f/validation_summary.json",
        }, indent=2))
        if result.get("status") == "COMPLETE_VS_MT5_OBSERVED":
            return 0
        if result.get("status") == "BLOCKED_SAFETY":
            return 3
        if result.get("status") == "FAILED_VALIDATION":
            return 2
        return 4
    except SafetyGateError as exc:
        print(json.dumps({"status": "BLOCKED_SAFETY", "run_id": run_id, "error_code": str(exc), "orders_sent": 0, "trades_executed": 0, "csv_written": False}, indent=2))
        try:
            summary = public_summary(ROOT, run_id)
            write_public_reports(ROOT, summary)
        except Exception:
            pass
        return 3
    except (CandleSyncError, OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "FAILED_VALIDATION", "run_id": run_id, "error_code": str(exc) if str(exc).isupper() else type(exc).__name__, "orders_sent": 0, "trades_executed": 0, "csv_written": False}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

