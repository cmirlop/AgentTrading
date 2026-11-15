"""Dry-run by default; future history reads require two explicit flags."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.market_history_backfill import (  # noqa: E402
    MAX_BARS_PER_REQUEST,
    MAX_PROVIDER_CALLS,
    _verify_terminal_gate,
    execute_backfill_plan,
    plan_chunks,
    validate_run_id,
    verify_real_readonly_account,
)
from src.history_data_integrity import CANONICAL_HASH_POLICY  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print planned bounded ranges; this is the default")
    mode.add_argument("--download", action="store_true", help="Explicitly read missing bars from an MT5 REAL terminal")
    parser.add_argument("--confirm-real-read-only", action="store_true", help="Required with --download; confirms the REAL account is tradeapi_disabled")
    parser.add_argument("--audit-plan", default="reports/phase2_2_2a/audits/future_mt5_backfill_plan.json")
    parser.add_argument("--run-id", help="Stable run identifier used to resume an interrupted versioned run")
    args = parser.parse_args(argv)
    if args.confirm_real_read_only and not args.download:
        parser.error("--confirm-real-read-only only applies with --download")
    try:
        plan_path = ROOT / args.audit_plan
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        if plan.get("schema_version") not in {2, 3} or plan.get("canonicalization_policy") != CANONICAL_HASH_POLICY:
            raise ValueError("BACKFILL_PLAN_SCHEMA_UNSUPPORTED")
        chunks = plan_chunks(
            plan.get("backfill_intervals", []),
            symbols=plan.get("symbols", ()),
            timeframes=plan.get("timeframes_seconds", {}),
            max_bars_per_request=plan.get("chunk_policy", {}).get("max_bars_per_request", MAX_BARS_PER_REQUEST),
            overlap_bars_each_side=plan.get("chunk_policy", {}).get("overlap_bars_each_side", 0),
        )
    except (OSError, ValueError, TypeError) as exc:
        code = str(exc) if str(exc).startswith("BACKFILL_") else "BACKFILL_PLAN_UNAVAILABLE_OR_INVALID"
        print(json.dumps({"status": "FAIL", "error_code": code}))
        return 2

    if not args.download:
        within_budget = len(chunks) <= MAX_PROVIDER_CALLS
        print(json.dumps({
            "status": "DRY_RUN" if within_budget else "PLAN_EXCEEDS_QUERY_BUDGET",
            "plan_status": plan.get("status"),
            "mt5_imported": False,
            "mt5_connections": 0,
            "orders_sent": 0,
            "provider_calls": 0,
            "max_bars_per_request": MAX_BARS_PER_REQUEST,
            "max_market_data_requests": MAX_PROVIDER_CALLS,
            "download_eligible": within_budget and plan.get("status") == "HISTORY_BACKFILL_PREPARED",
            "chunk_count": len(chunks),
            "calendar_review_intervals": plan.get("calendar_review_before_download", []),
            "chunks": chunks,
        }, indent=2))
        return 0 if within_budget else 3

    if not args.confirm_real_read_only:
        print(json.dumps({"status": "REJECTED", "error_code": "EXPLICIT_REAL_READ_ONLY_CONFIRMATION_REQUIRED"}))
        return 2
    if plan.get("status") != "HISTORY_BACKFILL_PREPARED" or plan.get("download_command_executed_in_this_phase") is not False:
        print(json.dumps({"status": "REJECTED", "error_code": "BACKFILL_PLAN_NOT_READY"}))
        return 2
    if len(chunks) > MAX_PROVIDER_CALLS:
        print(json.dumps({"status": "REJECTED", "error_code": "BACKFILL_PROVIDER_CALL_BUDGET_EXCEEDED", "planned_calls": len(chunks), "maximum_calls": MAX_PROVIDER_CALLS}))
        return 2
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        print(json.dumps({"status": "REJECTED", "error_code": str(exc)}))
        return 2

    mt5 = None
    initialized = False
    try:
        import MetaTrader5 as mt5_module  # Explicitly lazy: never imported by --dry-run.

        mt5 = mt5_module
        initialized = bool(mt5.initialize())
        if not initialized:
            raise RuntimeError("MT5_INITIALIZE_FAILED")
        gate = _verify_terminal_gate(mt5)
        account = verify_real_readonly_account(mt5)
        def report_progress(event: dict) -> None:
            try:
                print(json.dumps({"progress": event}), flush=True)
            except OSError:
                # A closed output stream must not change the read-only workflow.
                pass

        result = execute_backfill_plan(
            mt5,
            plan,
            root=ROOT,
            run_id=run_id,
            as_of_utc=datetime.now(timezone.utc),
            progress_callback=report_progress,
        )
        result["account_type"] = account["account_type"]
        result["read_only_safety"] = gate
        print(json.dumps({key: result.get(key) for key in ("status", "run_id", "provider_calls", "bars_written_this_run", "source_files_unchanged", "orders_sent", "tradeapi_disabled_verified")}, indent=2))
        return 0 if result["status"] == "HISTORY_BACKFILL_DOWNLOADED_PENDING_REAUDIT" else 3
    except Exception as exc:
        safe_error = str(exc)
        error_code = safe_error if safe_error.isascii() and safe_error.replace("_", "").isalnum() and len(safe_error) <= 96 else type(exc).__name__.upper()
        print(json.dumps({"status": "PENDING_OR_FAILED", "error_code": error_code, "orders_sent": 0}))
        return 3
    finally:
        if initialized and mt5 is not None:
            try:
                mt5.shutdown()
            except Exception:
                # No order or terminal configuration API is called during cleanup.
                pass


if __name__ == "__main__":
    raise SystemExit(main())
