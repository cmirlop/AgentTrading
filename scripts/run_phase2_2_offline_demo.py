#!/usr/bin/env python3
"""Deterministic end-to-end synthetic ledger and policy-risk demonstration."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from src.event_ledger import (  # noqa: E402
    append_outcome, bind_event, record_llm_call, record_llm_query_started,
    record_opportunity, record_system_decision, record_trade_context, reconcile_ledger,
)
from src.llm_evaluation import pair_ledger  # noqa: E402
from src.paired_policy_simulator import simulate_paired_policies  # noqa: E402
from src.research_policy import (  # noqa: E402
    CANONICAL_FILTER_ROLE, CANONICAL_MODEL, CANONICAL_PROVIDER,
    MARKET_ANALYST_FILTER_PROMPT, POLICY_VERSION,
    canonical_prompt_sha256,
)
from src.research_policy import normalize_technical_action  # noqa: E402


def _synthetic_trade_event(event_id="fixture", *, symbol="EURUSD", action="WATCH_LONG",
                           strategy="PULLBACK", net=2.0, cost=.25, decision_time=None):
    """Build a fully synthetic, future-labelled event without test imports."""
    base = decision_time or datetime(2026, 3, 1, tzinfo=timezone.utc)
    direction = "SELL" if normalize_technical_action(action) == "SHORT_CANDIDATE" else "BUY"
    entry = {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67}[symbol]
    stop = entry + .001 if direction == "SELL" else entry - .001
    release = base + timedelta(minutes=30)
    context = {
        "account_currency": "EUR",
        "symbol_metadata": {
            "EURUSD": {"currency_base": "EUR", "currency_profit": "USD", "trade_contract_size": 100000, "trade_tick_size": .00001, "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR", "volume_min": .01, "volume_max": 100, "volume_step": .01},
            "USDCAD": {"currency_base": "USD", "currency_profit": "CAD", "trade_contract_size": 100000, "trade_tick_size": .00001, "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR", "volume_min": .01, "volume_max": 100, "volume_step": .01},
            "AUDUSD": {"currency_base": "AUD", "currency_profit": "USD", "trade_contract_size": 100000, "trade_tick_size": .00001, "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR", "volume_min": .01, "volume_max": 100, "volume_step": .01},
        },
        "conversion_rates": {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67},
    }
    return {
        "event_id": event_id, "symbol": symbol, "decision_time_utc": base.isoformat(),
        "technical_decision": action, "technical_setup_quality": 80, "technical_overall_score": 40,
        "llm_decision": action if strategy != "NONE" else "NO_TRADE",
        "llm_model_output": strategy, "llm_role": CANONICAL_FILTER_ROLE,
        "provider": CANONICAL_PROVIDER, "model": CANONICAL_MODEL,
        "reported_model": CANONICAL_MODEL, "decision_policy_id": POLICY_VERSION,
        "prompt_sha256": canonical_prompt_sha256(),
        "llm_validation": "VALID", "llm_covered": True,
        "llm_coverage_status": "VALID_CANONICAL_RESPONSE", "risk_context": context,
        "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE",
        "plan": {"direction": direction, "entry": entry, "stop_loss": stop,
                 "take_profit": entry - .002 if direction == "SELL" else entry + .002,
                 "order_type": "MARKET"},
        "outcome": {
            "filled": True, "fill_time_utc": base.isoformat(),
            "release_time_utc": release.isoformat(), "known_at_utc": release.isoformat(),
            "expires_at_utc": None, "outcome_status": "RESOLVED",
            "result": "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT",
            "net_pnl_eur": net, "gross_pnl_eur": net + cost,
            "costs_eur": cost, "net_r": net / 5.0, "base_volume": .05,
            "cost_scenario": "CONSERVATIVE",
        },
    }


def _mock_model(strategy: str):
    return type("MockDecision", (), {"model_dump": lambda self, **kwargs: {
        "strategy": strategy, "market_regime": "TREND", "evidence_used": ["E1"],
    }})()


def _build_run(db_path: Path) -> dict:
    start = datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
    specifications = [
        ("fixture-1", "EURUSD", "WATCH_LONG", "PULLBACK", "BUY", 2.0),
        ("fixture-2", "USDCAD", "WATCH_SHORT", "NONE", "SELL", -2.0),
        ("fixture-3", "AUDUSD", "WATCH_LONG", "BREAKOUT", "HOLD", 1.0),
        ("fixture-4", "EURUSD", "WATCH_SHORT", "TIMEOUT", "WAIT", -1.0),
    ]
    simulator_events = []
    for index, (label, symbol, technical, strategy, taxonomy_action, net) in enumerate(specifications):
        decision_time = start + timedelta(minutes=index)
        snapshot = {
            "symbol": symbol, "analysis_time": decision_time.isoformat(),
            "technical_decision": technical,
            "technical_evaluation": {"setup_quality": 90-index*5, "overall_score": 40-index},
            "risk_context_summary": {"risk_policy_version": "config-risk-v1", "max_open_positions": 2,
                                     "active_slots": 0, "risk_approved": False, "rejection_codes": []},
        }
        event_id = record_opportunity(symbol, decision_time.isoformat(), snapshot,
            eligible=True, strategy_version="phase2.2-synthetic-v1", query_planned=False, path=db_path)
        payload = {"symbol": symbol, "analysis_time": decision_time.isoformat(), "allowed_evidence_ids": ["E1"]}
        with bind_event(event_id, db_path, agent_role=CANONICAL_FILTER_ROLE,
                        agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION):
            record_llm_query_started(role=CANONICAL_FILTER_ROLE, policy_id=POLICY_VERSION, payload=payload)
            if strategy == "TIMEOUT":
                record_llm_call(schema_name="StrategyAssessment", provider="deepseek", model="deepseek-flash",
                    prompt=MARKET_ANALYST_FILTER_PROMPT, payload=payload, allowed_evidence=["E1"],
                    requested_at=(decision_time+timedelta(seconds=1)).isoformat(), error_code="LLM_TIMEOUT",
                    agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT",
                    decision_policy_id=POLICY_VERSION, metadata={"schema_module": "src.agents.market_analyst", "provider_reported_model": "deepseek-flash"})
            else:
                record_llm_call(schema_name="StrategyAssessment", provider="deepseek", model="deepseek-flash",
                    prompt=MARKET_ANALYST_FILTER_PROMPT, payload=payload, allowed_evidence=["E1"],
                    requested_at=(decision_time+timedelta(seconds=1)).isoformat(), result=_mock_model(strategy),
                    agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT",
                    decision_policy_id=POLICY_VERSION, metadata={"schema_module": "src.agents.market_analyst", "provider_reported_model": "deepseek-flash"})
        # Persist diagnostic taxonomy fixtures too. They are deliberately not
        # canonical filter roles and can never be selected as the experiment vote.
        diagnostic_payload = {"event": label, "allowed_evidence_ids": ["E1"]}
        with bind_event(event_id, db_path, agent_role="SYNTHETIC_TAXONOMY_FIXTURE", agent_stage="TEST_ONLY"):
            record_llm_query_started(role="SYNTHETIC_TAXONOMY_FIXTURE", policy_id=None, payload=diagnostic_payload)
            record_llm_call(schema_name="SyntheticTaxonomy", provider="mock", model="offline",
                prompt="offline taxonomy fixture", payload=diagnostic_payload, allowed_evidence=["E1"],
                requested_at=(decision_time+timedelta(seconds=2)).isoformat(),
                result=type("Fixture", (), {"model_dump": lambda self, **kwargs: {"decision": taxonomy_action, "evidence_used": ["E1"]}})(),
                agent_role="SYNTHETIC_TAXONOMY_FIXTURE", agent_stage="TEST_ONLY")
        if label == "fixture-4":
            with bind_event(event_id, db_path, agent_role="SYNTHETIC_INVALID_FIXTURE", agent_stage="TEST_ONLY"):
                record_llm_query_started(role="SYNTHETIC_INVALID_FIXTURE", policy_id=None, payload={"evidence": ["E1"]})
                record_llm_call(schema_name="SyntheticInvalid", provider="mock", model="offline", prompt="offline",
                    payload={"evidence": ["E1"]}, allowed_evidence=["E1"],
                    requested_at=(decision_time+timedelta(seconds=3)).isoformat(), error_code="LLM_INVALID_EVIDENCE",
                    agent_role="SYNTHETIC_INVALID_FIXTURE", agent_stage="TEST_ONLY")
        final = technical if strategy not in {"NONE", "TIMEOUT"} else "NO_TRADE"
        record_system_decision(event_id, final, validation_status="INVALID" if strategy == "TIMEOUT" else "VALID",
            decision_stage="MARKET_ANALYSIS", agent_role=CANONICAL_FILTER_ROLE,
            decision_policy_id=POLICY_VERSION, path=db_path)

        candidate = _synthetic_trade_event(label, symbol=symbol, action=technical,
                           strategy=strategy if strategy != "TIMEOUT" else "PULLBACK",
                           net=net, cost=.25, decision_time=decision_time)
        candidate["event_id"] = event_id
        record_trade_context(event_id, {
            "candidate_status": "ELIGIBLE", "plan_status": "VALID",
            "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE",
            "plan": candidate["plan"], "risk_context": candidate["risk_context"],
            "market_data_time_utc": decision_time.isoformat(),
            "context_known_at_utc": decision_time.isoformat(),
            "risk_context_known_at_utc": decision_time.isoformat(),
        }, path=db_path)
        outcome = candidate["outcome"]
        outcome.update({"result": "WIN" if net > 0 else "LOSS", "outcome_status": "RESOLVED",
                        "net_pnl_eur": net, "gross_pnl_eur": net+.25, "costs_eur": .25,
                        "net_r": net/5.0, "cost_scenario": "CONSERVATIVE", "base_volume": .05})
        append_outcome(event_id, outcome["known_at_utc"], {"policy_id": "COMMON_PLAN_COUNTERFACTUAL", **outcome}, path=db_path)
        candidate["llm_validation"] = "INVALID" if strategy == "TIMEOUT" else "VALID"
        candidate["llm_covered"] = strategy != "TIMEOUT"
        candidate["llm_coverage_status"] = "PROVIDER_ERROR" if strategy == "TIMEOUT" else "VALID_CANONICAL_RESPONSE"
        candidate["llm_role"] = CANONICAL_FILTER_ROLE
        candidate["llm_model_output"] = strategy if strategy != "TIMEOUT" else "PULLBACK"
        simulator_events.append(candidate)

    joined = pair_ledger(db_path)
    reconciliation = reconcile_ledger(db_path)
    reconciliation["confirmatory_eligible"] = False
    reconciliation["eligibility_reason"] = "SYNTHETIC_FIXTURE_IS_ENGINEERING_EVIDENCE_ONLY"
    paired_by_id = {row["event_id"]: row for row in joined["population"]}
    reconciled_events = []
    for event in simulator_events:
        paired = paired_by_id[event["event_id"]]
        reconciled_events.append({**event,
            "llm_decision": paired.get("llm_decision"),
            "llm_model_output": paired.get("llm_model_output"),
            "llm_role": paired.get("llm_role"),
            "llm_validation": paired.get("llm_validation"),
            "llm_covered": paired.get("llm_covered"),
            "llm_coverage_status": paired.get("llm_coverage_status"),
            "provider": paired.get("provider"),
            "model": paired.get("model"),
            "decision_policy_id": paired.get("decision_policy_id"),
            "prompt_sha256": paired.get("prompt_sha256"),
            "plan": paired.get("plan"),
            "risk_context": paired.get("risk_context"),
        })
    policy_run = simulate_paired_policies(reconciled_events, cost_scenario="CONSERVATIVE", initial_equity_eur=10_000)
    return {
        "status": "PASS" if reconciliation["status"] == "PASS" and len(joined["population"]) == len(specifications) else "FAIL",
        "fixture_only": True,
        "opportunities": len(specifications),
        "taxonomy_outputs_logged": ["BUY", "SELL", "HOLD", "WAIT"],
        "invalid_evidence_logged": True,
        "timeout_logged": True,
        "pairing_counts": joined["counts"],
        "coverage_reconciliation": reconciliation,
        "risk_policy_simulation": policy_run,
        "real_deepseek_calls": 0,
        "real_ollama_calls": 0,
        "mt5_calls": 0,
        "orders_sent": 0,
    }


def _behavior_case(name: str, *, net: float, strategy: str) -> dict:
    event = _synthetic_trade_event(name, strategy=strategy, net=net, cost=.25,
                   decision_time=datetime(2026, 4, 1, tzinfo=timezone.utc))
    report = simulate_paired_policies([event], cost_scenario="CONSERVATIVE", initial_equity_eur=10_000)
    technical = report["policies"]["TECHNICAL_ONLY"]["net_pnl_eur"]
    deepseek = report["policies"]["TECHNICAL_PLUS_DEEPSEEK"]["net_pnl_eur"]
    difference = round(deepseek-technical, 8)
    behavior = "IMPROVEMENT_FIXTURE" if difference > 0 else "DEGRADATION_FIXTURE" if difference < 0 else "NO_CHANGE_FIXTURE"
    return {"scenario": name, "technical_only_net_eur": technical, "technical_plus_deepseek_net_eur": deepseek,
            "difference_eur": difference, "detected_behavior": behavior, "economic_evidence": False}


def run_demo() -> dict:
    with tempfile.TemporaryDirectory(prefix="phase22-ledger-demo-") as temp_dir:
        result = _build_run(Path(temp_dir) / "synthetic.sqlite3")
    with tempfile.TemporaryDirectory(prefix="phase22-ledger-repeat-") as temp_dir:
        repeated = _build_run(Path(temp_dir) / "synthetic.sqlite3")
    deterministic = result == repeated
    behaviors = [
        _behavior_case("improvement", net=-5, strategy="NONE"),
        _behavior_case("degradation", net=5, strategy="NONE"),
        _behavior_case("no-change", net=3, strategy="PULLBACK"),
    ]
    result["behavior_fixtures"] = behaviors
    result["reproducibility"] = {"status": "PASS" if deterministic else "FAIL",
                                  "same_input_same_output": deterministic,
                                  "provider_calls": 0}
    result["prompt_sha256"] = canonical_prompt_sha256()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "reports" / "phase2_2" / "synthetic_experiment.json")
    args = parser.parse_args()
    result = run_demo()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True, default=str)+"\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "events": result["opportunities"], "output": str(args.output),
                      "orders_sent": 0, "provider_calls": 0}, indent=2))
    return 0 if result["status"] == "PASS" and result["reproducibility"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
