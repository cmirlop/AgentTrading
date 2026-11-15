from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.backtest_config import CURRENT_LIVE_THRESHOLD
from config.market_config import SYMBOLS
from src.historical_execution import simulate_trade
from src.historical_risk import estimate_loss_eur
from src.historical_state import build_historical_market_state
from src.historical_trade_planner import build_historical_trade_plan
from src.verified_research_dataset import VerifiedResearchDataset


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_backtest(dataset: VerifiedResearchDataset, *, symbol: str, mode: str = "LIMIT",
                 cost_scenario: str = "BASE", spread_floor_points: float = 0.0) -> dict:
    if not dataset.public_manifest.get("economics_eligible"):
        return {
            "status": "NOT_RUN", "reason": "VERIFIED_SESSION_CALENDAR_REQUIRED",
            "dataset_fingerprint": dataset.public_manifest["dataset_fingerprint"],
            "trades_evaluated": 0,
        }
    scan = dataset.scan_symbol(symbol)
    dataset.assert_scan_parity(scan, symbol=symbol)
    prepared_by_symbol = {
        item: dataset.prepare_backtest_history(item) for item in SYMBOLS
    }
    prepared = prepared_by_symbol[symbol]
    eval_start = datetime.fromisoformat(dataset.public_manifest["evaluation_start_utc"])
    eval_end = datetime.fromisoformat(dataset.public_manifest["evaluation_end_utc"])
    observation_end = datetime.fromisoformat(dataset.public_manifest["observation_end_utc"])
    m5 = prepared["M5"]
    m5 = m5.loc[m5["bar_close"] <= observation_end].copy()
    if scan["rows"].empty:
        return {
            "status": "COMPLETED_NO_CANDIDATES", "dataset_fingerprint": dataset.public_manifest["dataset_fingerprint"],
            "evaluation_start_utc": eval_start.isoformat(), "evaluation_end_utc": eval_end.isoformat(),
            "trades_evaluated": 0, "candidate_rows": 0, "cost_scenario": cost_scenario,
            "spread_policy": dataset.public_manifest["spread_policy"],
        }
    candidate_rows = scan["rows"].loc[
        (scan["rows"]["setup_quality"] >= CURRENT_LIVE_THRESHOLD)
        & (scan["rows"]["alignment"] != "CONFLICT")
        & (scan["rows"]["bias"].isin(["BULLISH", "BEARISH"]))
    ]
    results = []
    for _, row in candidate_rows.iterrows():
        signal_time = row["analysis_time"]
        state = build_historical_market_state(symbol, signal_time, prepared)
        direction = "BUY" if row["bias"] == "BULLISH" else "SELL"
        plan = build_historical_trade_plan(symbol, direction, mode, state)
        if plan.get("status") != "VALID":
            results.append({"status": "PLAN_REJECTED"})
            continue
        risk_eur = estimate_loss_eur(plan, signal_time, prepared_by_symbol)
        execution = simulate_trade(
            plan, signal_time, m5, cost_scenario=cost_scenario,
            risk_amount_eur=risk_eur, spread_floor_points=spread_floor_points,
        )
        results.append({
            "status": execution.get("result"),
            "gross_r": execution.get("gross_r"), "net_r": execution.get("net_r"),
            "transaction_cost_r": execution.get("transaction_cost_r"),
            "spread_data_quality": execution.get("spread_data_quality"),
        })
    return {
        "status": "EXPLORATORY_NON_CONFIRMATORY",
        "dataset_fingerprint": dataset.public_manifest["dataset_fingerprint"],
        "evaluation_start_utc": eval_start.isoformat(), "evaluation_end_utc": eval_end.isoformat(),
        "observation_end_utc": observation_end.isoformat(),
        "candidate_rows": int(len(candidate_rows)), "trades_evaluated": len(results),
        "plan_rejected": sum(item["status"] == "PLAN_REJECTED" for item in results),
        "outcomes": results, "cost_scenario": cost_scenario,
        "spread_policy": dataset.public_manifest["spread_policy"],
        "frozen_scan_fallback_used": False, "data_market_fallback_used": False,
        "open_trades_forced_closed_at_evaluation_end": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a backtest only from a single verified research dataset.")
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--session-manifest", required=True, type=Path)
    parser.add_argument("--warmup-start-utc", required=True)
    parser.add_argument("--evaluation-start-utc", required=True)
    parser.add_argument("--evaluation-end-utc", required=True)
    parser.add_argument("--observation-end-utc", required=True)
    parser.add_argument("--as-of-utc", required=True)
    parser.add_argument("--symbol", required=True, choices=("EURUSD", "USDCAD", "AUDUSD"))
    parser.add_argument("--mode", default="LIMIT", choices=("LIMIT", "STOP"))
    parser.add_argument("--cost-scenario", default="BASE", choices=("BASE", "CONSERVATIVE", "STRESS"))
    parser.add_argument("--spread-floor-points", type=float, default=0.0)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print("dataset 1/3: loading manifests and verifying source bytes", flush=True)
    dataset = VerifiedResearchDataset.load_from_sources(
        ROOT, _read_json(args.source_manifest), _read_json(args.session_manifest),
        warmup_start_utc=args.warmup_start_utc, evaluation_start_utc=args.evaluation_start_utc,
        evaluation_end_utc=args.evaluation_end_utc, observation_end_utc=args.observation_end_utc,
        as_of_utc=args.as_of_utc, strategy_version=args.strategy_version,
        cost_scenario=args.cost_scenario,
    )
    print("dataset 2/3: generating scan from the verified frame set", flush=True)
    result = run_backtest(
        dataset, symbol=args.symbol, mode=args.mode, cost_scenario=args.cost_scenario,
        spread_floor_points=args.spread_floor_points,
    )
    print("dataset 3/3: backtest gate evaluated", flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = args.output.with_name(f".{args.output.name}.tmp")
        tmp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(args.output)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["status"] not in {"NOT_RUN"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
