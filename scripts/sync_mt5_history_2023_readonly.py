"""Audit, resume, apply and verify MT5 history from 2023 in REAL read-only mode."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.history_data_integrity import validate_run_id  # noqa: E402
from src.mt5_history_2023_sync import (  # noqa: E402
    DEFAULT_BATCH_CALLS,
    History2023Error,
    PRIVATE_BASE,
    PUBLIC_DOC,
    PUBLIC_JSON,
    SafetyGateError,
    _load_run,
    _write_json,
    apply_run,
    audit_provider,
    public_summary,
    verify_run,
    write_public_reports,
)


def _write_interrupted(root: Path, run_id: str, reason: str, status: str = "PARTIAL_EXECUTION_INTERRUPTED") -> dict | None:
    try:
        run_root, manifest, _inventory = _load_run(root, run_id)
        manifest["status"] = status
        manifest["interruption_reason"] = reason
        manifest["interrupted_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(root, run_root / "manifest.json", manifest)
        summary = public_summary(root, run_id)
        write_public_reports(root, summary)
        return summary
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--audit", action="store_true", help="Create a new run with a frozen UTC cutoff and audit MT5 ranges")
    mode.add_argument("--resume", action="store_true", help="Continue pending windows in the same run and cutoff")
    mode.add_argument("--apply", action="store_true", help="Reconcile verified provider bars into the 15 operational CSVs")
    mode.add_argument("--verify", action="store_true", help="Verify the second pass from cached evidence without downloads")
    parser.add_argument("--run-id", help="Required for --resume/--apply/--verify; optional for --audit")
    parser.add_argument("--batch-calls", type=int, default=DEFAULT_BATCH_CALLS, help="Maximum provider calls per visible progress batch (default 150); no total cap")
    args = parser.parse_args(argv)
    if args.batch_calls < 1 or args.batch_calls > 1000:
        parser.error("--batch-calls must be between 1 and 1000")
    if (args.resume or args.apply or args.verify) and not args.run_id:
        parser.error("--run-id is required with --resume, --apply, and --verify")

    as_of = datetime.now(timezone.utc) if args.audit else None
    run_id = args.run_id or as_of.strftime("%Y%m%dT%H%M%S%fZ")
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        parser.error(str(exc))

    if args.apply:
        try:
            summary = apply_run(ROOT, run_id)
        except SafetyGateError as exc:
            print(json.dumps({"status": "BLOCKED_SAFETY", "error_code": str(exc), "orders_sent": 0, "trades_executed": 0}, indent=2))
            return 3
        except (History2023Error, OSError, ValueError, KeyError) as exc:
            code = str(exc) if str(exc).isupper() else type(exc).__name__
            print(json.dumps({"status": "FAILED_VALIDATION", "error_code": code}, indent=2))
            return 2
        print(json.dumps({"status": summary["status"], "run_id": run_id, "report": PUBLIC_JSON.as_posix(), "orders_sent": 0, "trades_executed": 0}, indent=2))
        return 0 if summary["status"] == "COMPLETE_VS_MT5_2023_PLUS" else 4

    if args.verify:
        try:
            summary = verify_run(ROOT, run_id)
        except SafetyGateError as exc:
            print(json.dumps({"status": "BLOCKED_SAFETY", "error_code": str(exc)}, indent=2))
            return 3
        except (History2023Error, OSError, ValueError, KeyError) as exc:
            code = str(exc) if str(exc).isupper() else type(exc).__name__
            print(json.dumps({"status": "FAILED_VALIDATION", "error_code": code}, indent=2))
            return 2
        print(json.dumps({"status": summary["status"], "idempotency_second_pass": summary["idempotency_second_pass"], "idempotency_second_pass_new_bars": summary["idempotency_second_pass_new_bars"], "report": PUBLIC_JSON.as_posix()}, indent=2))
        return 0 if summary["idempotency_second_pass"] is True and summary["status"] == "COMPLETE_VS_MT5_2023_PLUS" else 4

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print(json.dumps({"status": "BLOCKED_SAFETY", "error_code": "METATRADER5_MODULE_UNAVAILABLE", "orders_sent": 0, "trades_executed": 0}, indent=2))
        return 3

    def progress(item: dict) -> None:
        print(json.dumps({"progress": item}, ensure_ascii=True), flush=True)

    initialized = False
    try:
        initialized = bool(mt5.initialize())
        if not initialized:
            # Still create and retain the original-file backups before the safety gate classifies PENDING.
            progress({"event": "MT5_INITIALIZE_FAILED", "action": "record_safe_block"})
        result = audit_provider(
            mt5,
            root=ROOT,
            run_id=run_id,
            as_of_utc=as_of,
            resume=args.resume,
            batch_call_limit=args.batch_calls,
            progress=progress,
        )
        summary = public_summary(ROOT, run_id)
        write_public_reports(ROOT, summary)
        print(json.dumps({
            "status": result.get("status"),
            "run_id": run_id,
            "as_of_utc": result.get("as_of_utc"),
            "provider_queries": result.get("provider_queries", 0),
            "batch_call_limit": args.batch_calls,
            "global_query_cap": None,
            "csv_written": False,
            "orders_sent": 0,
            "trades_executed": 0,
            "report": PUBLIC_JSON.as_posix(),
            "private_run": (PRIVATE_BASE / run_id).as_posix(),
        }, indent=2))
        if result.get("status") == "COMPLETE_VS_MT5_2023_PLUS":
            return 0
        if result.get("status") == "BLOCKED_SAFETY":
            return 3
        return 4
    except KeyboardInterrupt:
        summary = _write_interrupted(ROOT, run_id, "KEYBOARD_INTERRUPT_CHECKPOINTS_PRESERVED")
        print(json.dumps({"status": "PARTIAL_EXECUTION_INTERRUPTED", "run_id": run_id, "report": PUBLIC_JSON.as_posix() if summary else None, "orders_sent": 0, "trades_executed": 0}, indent=2))
        return 4
    except SafetyGateError as exc:
        print(json.dumps({"status": "BLOCKED_SAFETY", "run_id": run_id, "error_code": str(exc), "csv_written": False, "orders_sent": 0, "trades_executed": 0}, indent=2))
        _write_interrupted(ROOT, run_id, str(exc), status="BLOCKED_SAFETY")
        return 3
    except (History2023Error, OSError, ValueError, KeyError, RuntimeError) as exc:
        code = str(exc) if str(exc).isupper() else type(exc).__name__
        print(json.dumps({"status": "FAILED_VALIDATION", "run_id": run_id, "error_code": code, "orders_sent": 0, "trades_executed": 0}, indent=2))
        return 2
    finally:
        if initialized:
            mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

