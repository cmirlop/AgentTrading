#!/usr/bin/env python3
"""Deterministic offline demo of common-plan, paired-policy research.

All model answers and market outcomes are synthetic fixtures. This script does
not import MetaTrader5, contact an LLM provider, or execute orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from itertools import count
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.event_ledger import (  # noqa: E402
    append_outcome, bind_event, canonical_hash, record_llm_call,
    record_llm_query_started, record_opportunity, record_system_decision,
    record_technical_plan, record_trade_context, reconcile_ledger,
)
from src.llm_evaluation import compare_paired_policies, pair_ledger  # noqa: E402
from src.research_policy import (  # noqa: E402
    CANONICAL_FILTER_ROLE, CANONICAL_MODEL, CANONICAL_PROVIDER,
    MARKET_ANALYST_FILTER_PROMPT, POLICY_VERSION, canonical_prompt_sha256,
)
from src.technical_reference import POLICY_SHA256 as TECHNICAL_POLICY_SHA256  # noqa: E402
from src.paired_policy_simulator import simulate_paired_policies  # noqa: E402


def _risk_context() -> dict:
    metadata = {}
    for symbol, base, profit in (
        ("EURUSD", "EUR", "USD"), ("USDCAD", "USD", "CAD"), ("AUDUSD", "AUD", "USD"),
    ):
        metadata[symbol] = {
            "currency_base": base, "currency_profit": profit,
            "trade_contract_size": 100000, "trade_tick_size": .00001,
            "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR",
            "volume_min": .01, "volume_max": 100, "volume_step": .01,
        }
    return {
        "account_currency": "EUR", "symbol_metadata": metadata,
        "conversion_rates": {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67},
    }


def _fixture_plan(symbol: str, direction: str, entry: float) -> dict:
    stop = entry + .001 if direction == "SELL" else entry - .001
    target = entry - .0021 if direction == "SELL" else entry + .0021
    return {
        "symbol": symbol, "direction": direction, "order_type": "SELL_LIMIT" if direction == "SELL" else "BUY_LIMIT",
        "entry": entry, "stop_loss": stop, "take_profit": target,
        "mode": "LIMIT", "target_rr": 2.1,
    }


def _technical_record(event_id: str, symbol: str, action: str, decision: datetime, plan: dict) -> dict:
    return {
        "schema_version": "1.0", "policy_version": "TECHNICAL_REFERENCE_V1",
        "policy_sha256": TECHNICAL_POLICY_SHA256, "plan_source": "TECHNICAL_ONLY_REFERENCE",
        "status": "TECHNICAL_PLAN_VALID", "symbol": symbol,
        "decision_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "constructed_at_utc": (decision + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "maximum_market_data_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "technical_decision": action, "direction": plan["direction"], "plan": plan,
        "plan_sha256": canonical_hash(plan), "reason": "SYNTHETIC_TECHNICAL_REFERENCE_FIXTURE",
        "missing_fields": [], "spread_quality": "OBSERVED_VALID", "confirmatory_eligible": False,
        "technical_snapshot": {"bias": "BULLISH" if plan["direction"] == "BUY" else "BEARISH",
                               "alignment": "ALIGNED", "setup_quality": 80,
                               "overall_score": 40, "available_evidence_ids": ["E1"]},
    }


def _mock_response(strategy: str):
    return type("MockStrategyAssessment", (), {"model_dump": lambda self, **kwargs: {
        "strategy": strategy, "market_regime": "TREND", "evidence_used": ["E1"],
    }})()


def _build_run(db_path: Path) -> dict:
    start = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
    specifications = [
        ("approve", "EURUSD", "WATCH_LONG", "PULLBACK", 2.0, None),
        ("reject", "USDCAD", "WATCH_SHORT", "NONE", -2.0, None),
        ("approve-breakout", "AUDUSD", "WATCH_LONG", "BREAKOUT", 1.0, None),
        ("timeout", "EURUSD", "WATCH_SHORT", None, -1.0, "LLM_TIMEOUT"),
        ("invalid-response", "USDCAD", "WATCH_LONG", None, 1.5, "LLM_INVALID_EVIDENCE"),
    ]
    risk_context = _risk_context()
    for index, (label, symbol, action, strategy, net, error) in enumerate(specifications):
        decision = start + timedelta(minutes=index)
        direction = "BUY" if action == "WATCH_LONG" else "SELL"
        entry = {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67}[symbol]
        plan = _fixture_plan(symbol, direction, entry)
        event_snapshot = {
            "symbol": symbol, "analysis_time": decision.isoformat().replace("+00:00", "Z"),
            "technical_decision": action,
            "technical_evaluation": {"setup_quality": 80, "overall_score": 40,
                                      "bias": "BULLISH" if direction == "BUY" else "BEARISH",
                                      "alignment": "ALIGNED"},
        }
        with patch("src.event_ledger.utc_now", return_value=(decision + timedelta(seconds=0.5)).isoformat().replace("+00:00", "Z")):
            event_id = record_opportunity(
                symbol, decision, event_snapshot, eligible=True,
                strategy_version="phase2.2.1-synthetic-only", path=db_path,
            )

        # The technical plan is written before any mock answer is recorded.
        with patch("src.event_ledger.utc_now", return_value=(decision + timedelta(seconds=11)).isoformat().replace("+00:00", "Z")):
            plan_write = record_technical_plan(event_id, _technical_record(event_id, symbol, action, decision, plan), path=db_path)
        if not plan_write.persisted:
            raise RuntimeError(plan_write.error_code or "SYNTHETIC_PLAN_WRITE_FAILED")

        payload = {"symbol": symbol, "analysis_time": decision.isoformat(), "allowed_evidence_ids": ["E1"]}
        request_at = decision + timedelta(seconds=10)
        response_at = decision + timedelta(seconds=11)
        with patch("src.event_ledger.utc_now", return_value=response_at.isoformat().replace("+00:00", "Z")):
            with bind_event(event_id, db_path, agent_role=CANONICAL_FILTER_ROLE,
                            agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION):
                record_llm_query_started(role=CANONICAL_FILTER_ROLE, policy_id=POLICY_VERSION, payload=payload)
                result = record_llm_call(
                    schema_name="StrategyAssessment", provider=CANONICAL_PROVIDER, model=CANONICAL_MODEL,
                    prompt=MARKET_ANALYST_FILTER_PROMPT, payload=payload, allowed_evidence=["E1"],
                    requested_at=request_at.isoformat().replace("+00:00", "Z"),
                    result=_mock_response(strategy) if strategy is not None else None,
                    error_code=error, metadata={"schema_module": "src.agents.market_analyst",
                                                "provider_reported_model": CANONICAL_MODEL},
                    agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT",
                    decision_policy_id=POLICY_VERSION,
                )
                if not result.persisted:
                    raise RuntimeError(result.error_code or "SYNTHETIC_LLM_EVENT_WRITE_FAILED")
                valid = error is None
                final = action if valid and strategy != "NONE" else "NO_TRADE"
                record_system_decision(
                    event_id, final, validation_status="VALID" if valid else "INVALID",
                    decision_stage="MARKET_ANALYSIS",
                    rejection_code=None if valid else error,
                    agent_role=CANONICAL_FILTER_ROLE, decision_policy_id=POLICY_VERSION,
                    path=db_path,
                )

        context_known = decision + timedelta(seconds=12)
        with patch("src.event_ledger.utc_now", return_value=context_known.isoformat().replace("+00:00", "Z")):
            record_trade_context(event_id, {
                "candidate_status": "ELIGIBLE", "plan_status": "VALID",
                "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE", "plan": plan,
                "risk_context": risk_context,
                "market_data_time_utc": decision.isoformat().replace("+00:00", "Z"),
                "context_known_at_utc": context_known.isoformat().replace("+00:00", "Z"),
                "risk_context_known_at_utc": context_known.isoformat().replace("+00:00", "Z"),
            }, path=db_path)

        fill = decision + timedelta(seconds=15)
        close = decision + timedelta(minutes=30)
        observed = close
        persisted = close + timedelta(minutes=10)
        accepted = decision + timedelta(seconds=13)
        placed = decision + timedelta(seconds=14)
        outcome = {
            "policy_id": "COMMON_PLAN_COUNTERFACTUAL", "cost_scenario": "CONSERVATIVE",
            "result": "WIN" if net > 0 else "LOSS", "outcome_status": "RESOLVED",
            "filled": True, "fill_time_utc": fill.isoformat().replace("+00:00", "Z"),
            "accepted_at_utc": accepted.isoformat().replace("+00:00", "Z"),
            "placed_at_utc": placed.isoformat().replace("+00:00", "Z"),
            "economic_close_time_utc": close.isoformat().replace("+00:00", "Z"),
            "release_time_utc": close.isoformat().replace("+00:00", "Z"),
            "outcome_observed_at_utc": observed.isoformat().replace("+00:00", "Z"),
            "known_at_utc": observed.isoformat().replace("+00:00", "Z"),
            "persisted_at_utc": persisted.isoformat().replace("+00:00", "Z"),
            "net_pnl_eur": net, "gross_pnl_eur": net + .25, "costs_eur": .25,
            "net_r": net / 5.0, "base_volume": .05,
            "plan_sha256": canonical_hash(plan),
        }
        with patch("src.event_ledger.utc_now", return_value=persisted.isoformat().replace("+00:00", "Z")):
            append_outcome(event_id, outcome["outcome_observed_at_utc"], outcome, path=db_path)

    joined = pair_ledger(db_path)
    reconciliation = reconcile_ledger(db_path)
    reconciliation["confirmatory_eligible"] = False
    reconciliation["eligibility_reason"] = "SYNTHETIC_FIXTURE_IS_ENGINEERING_EVIDENCE_ONLY"
    comparison = compare_paired_policies(joined, cost_scenario="CONSERVATIVE", ledger_path=db_path,
                                         initial_equity_eur=10_000)
    return {
        "status": "PASS" if reconciliation["status"] == "PASS" and comparison["status"] == "OFFLINE_RISK_PARITY_SIMULATION" else "FAIL",
        "fixture_only": True, "opportunities": len(specifications),
        "mock_cases": ["APPROVE", "REJECT", "TIMEOUT", "INVALID_RESPONSE"],
        "coverage_reconciliation": reconciliation, "risk_policy_simulation": comparison,
        "same_plan_sha256_for_both_policies": comparison.get("common_plan_parity", {}).get("all_technical_candidates_share_one_plan_record") is True,
        "real_deepseek_calls": 0, "real_ollama_calls": 0,
        "mt5_connections": 0, "orders_sent": 0,
    }


def _behavior_case(name: str, *, net: float, strategy: str) -> dict:
    decision = datetime(2026, 4, 1, tzinfo=timezone.utc)
    event = _synthetic_trade_event(name, net=net, strategy=strategy, decision_time=decision)
    report = simulate_paired_policies([event], cost_scenario="CONSERVATIVE", initial_equity_eur=10_000)
    technical = report["policies"]["TECHNICAL_ONLY"]["net_pnl_eur"]
    deepseek = report["policies"]["TECHNICAL_PLUS_DEEPSEEK"]["net_pnl_eur"]
    delta = round(deepseek - technical, 8)
    label = "IMPROVEMENT_FIXTURE" if delta > 0 else "DEGRADATION_FIXTURE" if delta < 0 else "NO_CHANGE_FIXTURE"
    return {"scenario": name, "technical_only_net_eur": technical,
            "technical_plus_deepseek_net_eur": deepseek, "difference_eur": delta,
            "detected_behavior": label, "economic_evidence": False}


def _synthetic_trade_event(event_id: str, *, net: float, strategy: str, decision_time: datetime) -> dict:
    symbol = "EURUSD"
    plan = _fixture_plan(symbol, "BUY", 1.1)
    fill = decision_time + timedelta(seconds=10)
    close = decision_time + timedelta(minutes=30)
    response = decision_time + timedelta(seconds=5)
    return {
        "event_id": event_id, "symbol": symbol,
        "decision_time_utc": (decision_time + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
        "technical_decision": "WATCH_LONG", "technical_setup_quality": 80, "technical_overall_score": 40,
        "llm_decision": "WATCH_LONG" if strategy != "NONE" else "NO_TRADE",
        "llm_model_output": strategy, "llm_role": CANONICAL_FILTER_ROLE,
        "provider": CANONICAL_PROVIDER, "model": CANONICAL_MODEL,
        "reported_model": CANONICAL_MODEL, "decision_policy_id": POLICY_VERSION,
        "prompt_sha256": canonical_prompt_sha256(), "llm_validation": "VALID", "llm_covered": True,
        "llm_coverage_status": "VALID_CANONICAL_RESPONSE",
        "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE", "technical_plan_status": "TECHNICAL_PLAN_VALID",
        "technical_plan_version": "TECHNICAL_REFERENCE_V1",
        "technical_plan_policy_sha256": TECHNICAL_POLICY_SHA256, "plan": plan,
        "plan_sha256": canonical_hash(plan), "risk_context": _risk_context(),
        "outcome": {
            "filled": True, "fill_time_utc": fill.isoformat().replace("+00:00", "Z"),
            "accepted_at_utc": response.isoformat().replace("+00:00", "Z"),
            "placed_at_utc": response.isoformat().replace("+00:00", "Z"),
            "release_time_utc": close.isoformat().replace("+00:00", "Z"),
            "economic_close_time_utc": close.isoformat().replace("+00:00", "Z"),
            "known_at_utc": close.isoformat().replace("+00:00", "Z"),
            "outcome_observed_at_utc": close.isoformat().replace("+00:00", "Z"),
            "persisted_at_utc": (close + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
            "outcome_status": "RESOLVED", "result": "WIN" if net > 0 else "LOSS",
            "net_pnl_eur": net, "gross_pnl_eur": net + .25, "costs_eur": .25,
            "net_r": net / 5.0, "base_volume": .05, "cost_scenario": "CONSERVATIVE",
            "plan_sha256": canonical_hash(plan),
        },
    }


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="phase221-ledger-demo-") as temp_dir:
        counter = count(1)
        with patch("src.event_ledger.uuid4", side_effect=lambda: UUID(int=next(counter))):
            result = _build_run(Path(temp_dir) / "synthetic.sqlite3")
    with tempfile.TemporaryDirectory(prefix="phase221-ledger-repeat-") as temp_dir:
        counter = count(1)
        with patch("src.event_ledger.uuid4", side_effect=lambda: UUID(int=next(counter))):
            repeated = _build_run(Path(temp_dir) / "synthetic.sqlite3")
    deterministic = result == repeated
    behavior_fixtures = [
        _behavior_case("filter-avoids-loss", net=-5, strategy="NONE"),
        _behavior_case("filter-rejects-winner", net=5, strategy="NONE"),
        _behavior_case("filter-approves", net=3, strategy="PULLBACK"),
    ]
    result["behavior_fixtures"] = behavior_fixtures
    result["reproducibility"] = {"status": "PASS" if deterministic else "FAIL",
                                  "same_input_same_output": deterministic,
                                  "provider_calls": 0}
    result["prompt_sha256"] = canonical_prompt_sha256()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "phase2_2_1" / "synthetic_comparison.json")
    args = parser.parse_args()
    result = run_demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "events": result["opportunities"],
                      "output": str(args.output), "orders_sent": 0,
                      "provider_calls": 0, "reproducible": result["reproducibility"]["same_input_same_output"]}, indent=2))
    return 0 if result["status"] == "PASS" and result["reproducibility"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
