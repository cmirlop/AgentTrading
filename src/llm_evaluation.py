"""Offline provenance checks and same-opportunity event pairing."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd

from src.phase2_1_evaluation import utc_timestamp
from config import risk_config
from src.research_policy import (
    CANONICAL_FILTER_ROLE, CANONICAL_MODEL, CANONICAL_PROVIDER, POLICY_VERSION,
    canonical_prompt_sha256, deepseek_filter_outcome,
)


def load_pairing_tables(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    db = sqlite3.connect(str(path))
    try:
        opportunities = pd.read_sql_query("SELECT * FROM opportunities", db)
        llm = pd.read_sql_query("SELECT * FROM llm_events", db)
        systems = pd.read_sql_query("SELECT * FROM system_events", db)
        outcomes = pd.read_sql_query("SELECT * FROM outcomes", db)
        coverage = pd.read_sql_query("SELECT * FROM coverage_events", db)
        contexts = pd.read_sql_query("SELECT * FROM trade_contexts", db)
    finally:
        db.close()
    return opportunities, llm, systems, outcomes, coverage, contexts


def _event_checks(opportunities: pd.DataFrame, llm: pd.DataFrame, systems: pd.DataFrame,
                  outcomes: pd.DataFrame, coverage: pd.DataFrame,
                  contexts: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    counts = {
        "technical_events_total": int(len(opportunities)),
        "llm_events_total": int(len(llm)),
        "llm_valid": int(llm["pydantic_status"].eq("VALID").sum()) if len(llm) else 0,
        "llm_invalid": int(llm["pydantic_status"].ne("VALID").sum()) if len(llm) else 0,
        "llm_none": 0, "llm_wait": 0, "llm_buy_sell": 0,
        "llm_filter_approved": 0, "llm_invalid_action_taxonomy": 0,
        "llm_strategy_declined": 0,
        "matching_decisions": 0, "disagreeing_decisions": 0,
        "events_without_llm_coverage": 0, "duplicate_event_ids": 0,
        "multiple_canonical_calls": 0, "unverified_provenance": 0,
        "provider_errors": int(llm["error_code"].notna().sum()) if len(llm) else 0,
        "outcome_events": int(outcomes["event_id"].nunique()) if len(outcomes) else 0,
        "trade_contexts_missing_or_invalid": 0,
    }
    if not len(opportunities):
        return pd.DataFrame(), counts
    if opportunities["event_id"].duplicated().any():
        counts["duplicate_event_ids"] += int(opportunities["event_id"].duplicated(keep=False).sum())
    if len(llm) and llm["llm_event_id"].duplicated().any():
        counts["duplicate_event_ids"] += int(llm["llm_event_id"].duplicated(keep=False).sum())

    llm_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in llm.to_dict("records"):
        llm_by_event.setdefault(str(row["event_id"]), []).append(row)
    systems_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in systems.to_dict("records"):
        if row.get("decision_stage") == "MARKET_ANALYSIS" and row.get("agent_role") == CANONICAL_FILTER_ROLE:
            systems_by_event.setdefault(str(row["event_id"]), []).append(row)
    coverage_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in coverage.to_dict("records"):
        if row.get("event_id"):
            coverage_by_event.setdefault(str(row["event_id"]), []).append(row)
    contexts_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in contexts.to_dict("records"):
        contexts_by_event.setdefault(str(row["event_id"]), []).append(row)

    population_rows = []
    for opportunity in opportunities.to_dict("records"):
        event_id = str(opportunity["event_id"])
        technical = json.loads(opportunity["snapshot_json"])
        technical_decision = str(technical.get("technical_decision", technical.get("decision", ""))).upper()
        technical_evaluation = technical.get("technical_evaluation") or {}
        context_rows = contexts_by_event.get(event_id, [])
        trade_context = {}
        if len(context_rows) == 1:
            try:
                trade_context = json.loads(context_rows[0]["context_json"])
            except (TypeError, json.JSONDecodeError):
                trade_context = {}
        elif not context_rows:
            trade_context = {
                "plan": technical.get("plan"),
                "risk_context": technical.get("risk_context"),
            }
        outcome_rows = outcomes[outcomes["event_id"].astype(str).eq(event_id)] if len(outcomes) else pd.DataFrame()
        event_outcomes = []
        decision_timestamp = utc_timestamp(opportunity["decision_time_utc"], field="decision_time")
        for outcome_row in outcome_rows.to_dict("records"):
            try:
                known_at = utc_timestamp(outcome_row["known_at_utc"], field="known_at")
                record = json.loads(outcome_row["outcome_json"])
                release = record.get("release_time_utc")
                if known_at < decision_timestamp or (release and utc_timestamp(release, field="release_time") < decision_timestamp):
                    counts["unverified_provenance"] += 1
                    continue
                event_outcomes.append(record)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                counts["unverified_provenance"] += 1

        all_events = llm_by_event.get(event_id, [])
        role_events = [row for row in all_events if row.get("agent_role") == CANONICAL_FILTER_ROLE]
        coverage_rows = coverage_by_event.get(event_id, [])
        base = {
            "event_id": event_id, "symbol": opportunity["symbol"],
            "opportunity_time_utc": opportunity["decision_time_utc"],
            "decision_time_utc": opportunity["decision_time_utc"],
            "opportunity_id_version": opportunity.get("opportunity_id_version"),
            "opportunity_payload_sha256": opportunity.get("payload_sha256"),
            "strategy_version": opportunity.get("strategy_version"),
            "config_sha256": opportunity.get("config_sha256"),
            "code_revision": opportunity.get("code_revision"),
            "technical_decision": technical_decision or None,
            "technical_setup_quality": technical_evaluation.get("setup_quality", technical.get("setup_quality")),
            "technical_overall_score": technical_evaluation.get("overall_score", technical.get("overall_score")),
            "plan": trade_context.get("plan"),
            "risk_context": trade_context.get("risk_context"),
            "trade_context_sha256": context_rows[0].get("payload_sha256") if len(context_rows) == 1 else None,
            "plan_strategy_source": trade_context.get("plan_strategy_source"),
            "trade_context_status": "MISSING",
            "outcomes": event_outcomes,
        }
        if len(role_events) != 1:
            counts["events_without_llm_coverage"] += 1
            if len(role_events) > 1:
                counts["multiple_canonical_calls"] += 1
                coverage_status = "MULTIPLE_CANONICAL_ROLE_CALLS"
                validation = "AMBIGUOUS_MULTIPLE_CALLS"
            elif all_events:
                coverage_status = "NON_CANONICAL_AGENT_CALLS_ONLY"
                validation = "NO_CANONICAL_ROLE_COVERAGE"
            elif any(row.get("event_type") == "LLM_ERROR_RECEIVED" for row in coverage_rows):
                coverage_status = "PROVIDER_ERROR"
                validation = "INVALID"
            else:
                coverage_status = "NO_QUERY_OR_NO_CANONICAL_COVERAGE"
                validation = "NO_COVERAGE"
            if role_events or all_events:
                counts["unverified_provenance"] += 1
            population_rows.append({**base, "llm_decision": None, "llm_model_output": None,
                "llm_role": CANONICAL_FILTER_ROLE, "provider": None, "model": None,
                "prompt_sha256": None, "payload_sha256": None, "llm_validation": validation,
                "llm_coverage_status": coverage_status, "llm_covered": False,
                "decision_match": None})
            continue

        event = role_events[0]
        try:
            request = utc_timestamp(event["requested_at_utc"], field="requested_at")
            response = utc_timestamp(event["responded_at_utc"], field="responded_at")
            allowed = set(json.loads(event["allowed_evidence_json"]))
            decision = json.loads(event["decision_json"] or "{}")
            used = decision.get("evidence_used", decision.get("evidence_ids", []))
            model_strategy = str(decision.get("strategy", "")).upper()
            provenance_ok = (
                event["provider"] == CANONICAL_PROVIDER
                and event["configured_model"] == CANONICAL_MODEL
                and event.get("reported_model") == CANONICAL_MODEL
                and event.get("decision_policy_id") == POLICY_VERSION
                and event["prompt_sha256"] == canonical_prompt_sha256()
                and len(event["payload_sha256"]) == 64
                and request >= decision_timestamp and response >= request
                and event["pydantic_status"] == "VALID"
                and any(row.get("event_type") == "LLM_QUERY_SENT" and row.get("payload_sha256") == event["payload_sha256"] for row in coverage_rows)
                and any(row.get("event_type") == "LLM_RESPONSE_PERSISTED" and row.get("payload_sha256") == event["payload_sha256"] for row in coverage_rows)
                and isinstance(used, list) and all(item in allowed for item in used)
                and bool(model_strategy)
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            provenance_ok = False
            decision = {}
            model_strategy = ""
        if not provenance_ok:
            counts["events_without_llm_coverage"] += 1
            counts["unverified_provenance"] += 1
            population_rows.append({**base, "llm_decision": None, "llm_model_output": model_strategy or None,
                "llm_role": CANONICAL_FILTER_ROLE, "provider": event.get("provider"),
                "model": event.get("configured_model"), "prompt_sha256": event.get("prompt_sha256"),
                "payload_sha256": event.get("payload_sha256"), "llm_validation": "INVALID_OR_UNVERIFIED",
                "llm_coverage_status": "CANONICAL_RESPONSE_INVALID_OR_UNVERIFIED",
                "llm_covered": False, "decision_match": None})
            continue

        system_rows = systems_by_event.get(event_id, [])
        system_valid = False
        if len(system_rows) == 1:
            try:
                system_valid = (
                    system_rows[0]["validation_status"] == "VALID"
                    and utc_timestamp(system_rows[0]["created_at_utc"], field="system_decision_time") >= response
                )
            except (ValueError, TypeError, KeyError):
                system_valid = False
        if not system_valid:
            counts["events_without_llm_coverage"] += 1
            counts["unverified_provenance"] += 1
            population_rows.append({**base, "llm_decision": None, "llm_model_output": model_strategy,
                "llm_role": CANONICAL_FILTER_ROLE, "provider": event["provider"],
                "model": event["configured_model"], "prompt_sha256": event["prompt_sha256"],
                "payload_sha256": event["payload_sha256"], "llm_validation": "NO_VALID_SYSTEM_DECISION",
                "llm_coverage_status": "SYSTEM_DECISION_MISSING_OR_INVALID", "llm_covered": False,
                "decision_match": None})
            continue

        # The source candle must not be newer than the opportunity. Market
        # metadata and plan inputs must be timestamped and known before the
        # common simulated decision time.
        context_times_valid = False
        market_time = context_known = risk_known = None
        if len(context_rows) == 1:
            try:
                market_time = utc_timestamp(trade_context["market_data_time_utc"], field="market_data_time")
                context_known = utc_timestamp(trade_context["context_known_at_utc"], field="context_known_at")
                risk_known = utc_timestamp(trade_context["risk_context_known_at_utc"], field="risk_context_known_at")
                context_times_valid = market_time <= decision_timestamp and context_known >= market_time and risk_known >= market_time
            except (ValueError, TypeError, KeyError):
                context_times_valid = False
        if context_times_valid:
            effective_decision = max(
                response, utc_timestamp(system_rows[0]["created_at_utc"], field="system_decision_time"),
                context_known, risk_known,
            )
            base["decision_time_utc"] = effective_decision.isoformat().replace("+00:00", "Z")
            base["trade_context_status"] = "TIMESTAMPS_VERIFIED"
        else:
            counts["trade_contexts_missing_or_invalid"] += 1
            base["plan"] = None
            base["risk_context"] = None
            base["plan_strategy_source"] = "UNVERIFIED_CONTEXT"
            base["trade_context_status"] = "MISSING_OR_FUTURE_DATA_CONTEXT"

        llm_decision = str(system_rows[0]["final_decision"]).upper()
        approved, reason = deepseek_filter_outcome(technical_decision, model_strategy, role=CANONICAL_FILTER_ROLE)
        expected_final = technical_decision if approved else "NO_TRADE"
        final_consistent = llm_decision == expected_final
        if not final_consistent:
            counts["events_without_llm_coverage"] += 1
            counts["unverified_provenance"] += 1
        counts["llm_none"] += int(model_strategy == "NONE")
        counts["llm_wait"] += int(model_strategy == "WAIT")
        counts["llm_filter_approved"] += int(approved)
        counts["llm_strategy_declined"] += int(model_strategy == "NONE")
        counts["llm_invalid_action_taxonomy"] += int(model_strategy in {"BUY", "SELL", "HOLD", "WAIT"})
        same = bool(technical_decision) and technical_decision == llm_decision
        counts["matching_decisions"] += int(bool(same))
        counts["disagreeing_decisions"] += int(bool(technical_decision) and not same)
        population_rows.append({**base, "llm_decision": llm_decision,
            "llm_model_output": model_strategy, "llm_filter_approved": approved,
            "llm_filter_reason": reason, "llm_role": event["agent_role"],
            "decision_policy_id": event.get("decision_policy_id"),
            "provider": event["provider"], "model": event["configured_model"],
            "reported_model": event.get("reported_model"),
            "prompt_sha256": event["prompt_sha256"], "payload_sha256": event["payload_sha256"],
            "llm_validation": event["pydantic_status"],
            "llm_coverage_status": "VALID_CANONICAL_RESPONSE" if final_consistent else "SYSTEM_DECISION_MISMATCH",
            "llm_covered": bool(final_consistent), "decision_match": bool(same)})
    return pd.DataFrame(population_rows), counts


def pair_ledger(path: str | Path) -> dict[str, Any]:
    opportunities, llm, systems, outcomes, coverage, contexts = load_pairing_tables(path)
    population, counts = _event_checks(opportunities, llm, systems, outcomes, coverage, contexts)
    paired = population.loc[population["llm_covered"]] if len(population) else population
    return {
        "schema_version": "2.0",
        "status": "PAIRING_COVERAGE_AVAILABLE" if len(paired) else "NO_VERIFIABLE_PAIRS",
        "counts": counts,
        "pairs": paired.to_dict("records"),
        "population": population.to_dict("records"),
        "system_events_total": int(len(systems)),
        "coverage_events_total": int(len(coverage)),
        "trade_contexts_total": int(len(contexts)),
        "limitations": [
            "Only events carrying valid provider, model, prompt, payload, evidence and UTC provenance are eligible.",
            "Multiple valid LLM calls for one opportunity are excluded until a preregistered decision role is supplied.",
            "No missing historical model decisions are synthesized.",
            "Economic comparison requires independently recorded policy outcomes for both policies and portfolio constraints.",
        ],
    }


def compare_paired_policies(paired_events: list[dict[str, Any]] | dict[str, Any], *, cost_scenario: str = "CONSERVATIVE",
                            max_open_positions: int | None = None, initial_equity_eur: float = 10_000.0,
                            state_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the complete paired population through independent risk states.

    ``max_open_positions`` is accepted only for compatibility; overrides are
    rejected so this evaluator cannot drift from config/risk_config.py. A raw
    list is rejected because it cannot prove that uncovered or rejected events
    were not dropped before the economic comparison.
    """
    if max_open_positions is not None and max_open_positions != risk_config.MAX_OPEN_POSITIONS:
        return {
            "status": "NO_EVALUABLE_RISK_CONFIG_OVERRIDE",
            "risk_limit_source": "config.risk_config.MAX_OPEN_POSITIONS",
            "configured_max_open_positions": risk_config.MAX_OPEN_POSITIONS,
            "requested_max_open_positions": max_open_positions,
            "economic_inference_allowed": False,
        }
    if not isinstance(paired_events, dict):
        return {
            "status": "NO_EVALUABLE",
            "reason_code": "POPULATION_PROVENANCE_REQUIRED",
            "reason": "Pass the full pair_ledger report, including population and reconciliation counts.",
            "economic_inference_allowed": False,
        }
    population = paired_events.get("population")
    counts = paired_events.get("counts")
    if (not isinstance(population, list) or not isinstance(counts, dict)
            or counts.get("technical_events_total") != len(population)
            or not population
            or len({str(row.get("event_id")) for row in population if isinstance(row, dict)}) != len(population)):
        return {
            "status": "NO_EVALUABLE_POPULATION_COVERAGE",
            "reason": "Full population coverage is missing, incomplete or contains duplicate events.",
            "economic_inference_allowed": False,
        }
    from src.paired_policy_simulator import simulate_paired_policies
    normalized_events = []
    for event in population:
        normalized = dict(event)
        if "outcome" not in normalized and isinstance(normalized.get("outcomes"), list):
            common = [row for row in normalized["outcomes"]
                      if isinstance(row, dict)
                      and row.get("policy_id") == "COMMON_PLAN_COUNTERFACTUAL"
                      and str(row.get("cost_scenario", "")).upper() == str(cost_scenario).upper()]
            if len(common) == 1:
                normalized["outcome"] = common[0]
            elif len(common) > 1:
                normalized["outcome"] = None
        normalized_events.append(normalized)
    return simulate_paired_policies(
        normalized_events, cost_scenario=cost_scenario,
        initial_equity_eur=initial_equity_eur, state_dir=state_dir,
    )
