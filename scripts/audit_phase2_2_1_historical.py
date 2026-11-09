#!/usr/bin/env python3
"""Read-only comparison of TECHNICAL_REFERENCE_V1 with the frozen scan/backtest."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.event_ledger import canonical_hash  # noqa: E402
from src.historical_state import prepare_symbol_history, build_historical_market_state  # noqa: E402
from src.technical_reference import build_technical_reference_plan  # noqa: E402


SCAN_PATH = ROOT / "data" / "backtest" / "technical_candidate_scan.csv"
STATEFUL_TRADES_PATH = ROOT / "data" / "backtest" / "stateful_trades.csv"
EXPECTED_SCAN_SHA256 = "0ee396b59b515446d92bfd9293e2395e4ebf8dca0de8d86fac2ba3bdffe856c3"


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit() -> dict:
    import pandas as pd

    started = monotonic()
    scan_hash = _sha256(SCAN_PATH)
    if scan_hash != EXPECTED_SCAN_SHA256:
        return {"status": "FAIL", "reason": "FROZEN_SCAN_HASH_MISMATCH",
                "expected_sha256": EXPECTED_SCAN_SHA256, "actual_sha256": scan_hash}
    scan = pd.read_csv(SCAN_PATH)
    scan["analysis_time"] = pd.to_datetime(scan["analysis_time"], utc=True)
    symbols = sorted(scan["symbol"].astype(str).str.upper().unique())
    prepared = {symbol: prepare_symbol_history(symbol) for symbol in symbols}
    plan_rows: dict[tuple[str, str], dict] = {}
    statuses = Counter()
    rejection_reasons = Counter()
    symbol_counts: dict[str, Counter] = defaultdict(Counter)
    candidate_disagreements = []
    score_disagreements = 0
    score_difference_samples = []
    alignment_disagreements = 0
    direction_disagreements = 0
    source_time_violations = 0
    zero_spread_candidates = 0
    source_failures = []
    total = len(scan)

    for row in scan.itertuples(index=False):
        symbol = str(row.symbol).upper()
        decision = row.analysis_time.to_pydatetime().astimezone(timezone.utc)
        decision_string = decision.isoformat().replace("+00:00", "Z")
        try:
            state = build_historical_market_state(symbol, decision, prepared[symbol])
            result = build_technical_reference_plan(
                symbol, state, decision_string,
                constructed_at_utc=(decision + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            )
        except Exception as exc:
            result = {"status": "TECHNICAL_PLAN_NOT_EVALUABLE", "reason": f"{type(exc).__name__}:{exc}",
                      "technical_snapshot": None, "plan": None, "plan_sha256": None,
                      "maximum_market_data_time_utc": None, "spread_quality": "MISSING",
                      "confirmatory_eligible": False}
            source_failures.append({"symbol": symbol, "analysis_time": decision_string,
                                    "reason": str(result["reason"])[:180]})
        status = str(result.get("status", "UNKNOWN"))
        statuses[status] += 1
        symbol_counts[symbol][status] += 1
        if status == "TECHNICAL_PLAN_NOT_EVALUABLE":
            rejection_reasons[str(result.get("reason", "UNKNOWN"))] += 1
        expected_candidate = bool(row.candidate_65)
        actual_candidate = status != "NO_TECHNICAL_CANDIDATE"
        if expected_candidate != actual_candidate:
            candidate_disagreements.append({"symbol": symbol, "analysis_time": decision_string,
                "baseline_candidate_65": expected_candidate, "reference_status": status,
                "reason": result.get("reason")})
        technical = result.get("technical_snapshot") or {}
        try:
            if abs(float(technical.get("setup_quality")) - float(row.setup_quality)) > 1e-9:
                score_disagreements += 1
                if len(score_difference_samples) < 20:
                    score_difference_samples.append({
                        "symbol": symbol, "analysis_time": decision_string,
                        "frozen_setup_quality": float(row.setup_quality),
                        "reconstructed_setup_quality": technical.get("setup_quality"),
                        "bias_frozen": str(row.bias), "bias_reconstructed": technical.get("bias"),
                    })
        except (TypeError, ValueError):
            score_disagreements += 1
        if str(technical.get("alignment", "")).upper() != str(row.alignment).upper():
            alignment_disagreements += 1
        if str(technical.get("bias", "")).upper() != str(row.bias).upper():
            direction_disagreements += 1
        maximum = result.get("maximum_market_data_time_utc")
        if maximum and datetime.fromisoformat(maximum.replace("Z", "+00:00")) > decision:
            source_time_violations += 1
        if result.get("spread_quality") == "OBSERVED_ZERO_UNVERIFIED" and expected_candidate:
            zero_spread_candidates += 1
        plan_rows[(symbol, decision_string)] = result

    saved_trades = pd.read_csv(STATEFUL_TRADES_PATH) if STATEFUL_TRADES_PATH.is_file() else pd.DataFrame()
    if len(saved_trades):
        saved_trades = saved_trades[
            pd.to_numeric(saved_trades["threshold"], errors="coerce").eq(65)
            & saved_trades["mode"].astype(str).str.upper().eq("LIMIT")
        ].copy()
    trade_plan_matches = 0
    trade_plan_mismatches = []
    trade_plan_unavailable = 0
    if len(saved_trades):
        for trade in saved_trades.itertuples(index=False):
            timestamp = pd.to_datetime(trade.signal_time, utc=True).isoformat().replace("+00:00", "Z")
            generated = plan_rows.get((str(trade.symbol).upper(), timestamp))
            if not generated or generated.get("status") != "TECHNICAL_PLAN_VALID":
                trade_plan_unavailable += 1
                trade_plan_mismatches.append({"symbol": str(trade.symbol).upper(), "signal_time": timestamp,
                    "reason": "REFERENCE_PLAN_NOT_VALID_FOR_SAVED_TRADE"})
                continue
            plan = generated["plan"]
            values = (float(trade.entry), float(trade.stop_loss), float(trade.take_profit))
            reconstructed = (float(plan["entry"]), float(plan["stop_loss"]), float(plan["take_profit"]))
            if str(trade.mode).upper() != "LIMIT" or any(abs(a - b) > 1e-10 for a, b in zip(values, reconstructed)):
                trade_plan_mismatches.append({"symbol": str(trade.symbol).upper(), "signal_time": timestamp,
                    "saved_mode": str(trade.mode), "saved_prices": list(values),
                    "reference_prices": list(reconstructed), "reason": "SAVED_PLAN_FIELDS_DIFFER"})
            else:
                trade_plan_matches += 1

    candidate_count = int(scan["candidate_65"].astype(bool).sum())
    return {
        "schema_version": "2.2.1",
        "status": "PASS" if not source_failures and not source_time_violations and not trade_plan_mismatches else "FAIL",
        "confirmatory_status": "NOT_CONFIRMATORY_EXPLORATORY_HISTORY_ONLY",
        "reference_policy": "TECHNICAL_REFERENCE_V1",
        "input": {"scan_path": str(SCAN_PATH.relative_to(ROOT)), "scan_sha256": scan_hash,
                  "expected_scan_sha256": EXPECTED_SCAN_SHA256, "scan_rows": total,
                  "candidate_65_rows": candidate_count,
                  "stateful_trades_path": str(STATEFUL_TRADES_PATH.relative_to(ROOT)),
                  "stateful_trades_sha256": _sha256(STATEFUL_TRADES_PATH),
                  "stateful_trade_rows": len(saved_trades)},
        "reconstruction": {
            "status_counts": dict(sorted(statuses.items())),
            "by_symbol": {symbol: dict(sorted(counts.items())) for symbol, counts in sorted(symbol_counts.items())},
            "baseline_candidate_disagreements": len(candidate_disagreements),
            "setup_quality_disagreements": score_disagreements,
            "first_setup_quality_differences": score_difference_samples,
            "alignment_disagreements": alignment_disagreements,
            "bias_disagreements": direction_disagreements,
            "source_timestamp_violations": source_time_violations,
            "candidate_plans_with_unverified_zero_spread": zero_spread_candidates,
            "plan_rejection_reasons": dict(rejection_reasons.most_common()),
            "data_reconstruction_errors": len(source_failures),
            "first_candidate_disagreements": candidate_disagreements[:20],
            "first_data_errors": source_failures[:20],
        },
        "compatibility_with_saved_stateful_65_limit_trades": {
            "matched_entry_sl_tp_and_mode": trade_plan_matches,
            "unavailable_or_mismatched": len(trade_plan_mismatches),
            "unavailable_plan_count": trade_plan_unavailable,
            "first_differences": trade_plan_mismatches[:20],
            "economic_results_compared": False,
        },
        "baseline_signal_differences_are_recorded_not_forced_to_match": True,
        "plan_construction_uses_future_outcomes": False,
        "outcomes_used_for_plan_construction": False,
        "economic_conclusion": "INCONCLUSIVE_NOT_A_CONFIRMATORY_BACKTEST",
        "duration_seconds": round(monotonic() - started, 3),
        "dataset_sha256": scan_hash,
        "reconstruction_sha256": canonical_hash({
            "statuses": dict(sorted(statuses.items())),
            "candidate_disagreements": candidate_disagreements,
            "trade_plan_matches": trade_plan_matches,
            "trade_plan_mismatches": trade_plan_mismatches,
        }),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=ROOT / "reports" / "phase2_2_1" / "historical_compatibility.json")
    args = parser.parse_args()
    result = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "scan_rows": result.get("input", {}).get("scan_rows"),
                      "candidate_disagreements": result.get("reconstruction", {}).get("baseline_candidate_disagreements"),
                      "saved_plan_matches": result.get("compatibility_with_saved_stateful_65_limit_trades", {}).get("matched_entry_sl_tp_and_mode"),
                      "duration_seconds": result.get("duration_seconds"), "report": str(args.output)}, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
