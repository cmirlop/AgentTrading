"""Offline provenance checks and same-opportunity event pairing."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd

from src.phase2_1_evaluation import utc_timestamp
from config import risk_config
from src.event_ledger import canonical_hash, initialize_ledger, ledger_content_sha256, reconcile_ledger
from src.technical_reference import POLICY_SHA256 as TECHNICAL_REFERENCE_POLICY_SHA256
from src.research_policy import (
    CANONICAL_FILTER_ROLE, CANONICAL_MODEL, CANONICAL_PROVIDER, POLICY_VERSION,
    canonical_prompt_sha256, deepseek_filter_outcome,
)


def load_pairing_tables(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    initialize_ledger(path)
    db = sqlite3.connect(str(path))
    try:
        opportunities = pd.read_sql_query("SELECT * FROM opportunities", db)
        llm = pd.read_sql_query("SELECT * FROM llm_events", db)
        systems = pd.read_sql_query("SELECT * FROM system_events", db)
        outcomes = pd.read_sql_query("SELECT * FROM outcomes", db)
        coverage = pd.read_sql_query("SELECT * FROM coverage_events", db)
        contexts = pd.read_sql_query("SELECT * FROM trade_contexts", db)
        plans = pd.read_sql_query("SELECT * FROM technical_plans", db)
    finally:
        db.close()
    return opportunities, llm, systems, outcomes, coverage, contexts, plans


def _event_checks(opportunities: pd.DataFrame, llm: pd.DataFrame, systems: pd.DataFrame,
                  outcomes: pd.DataFrame, coverage: pd.DataFrame,
                  contexts: pd.DataFrame, plans: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
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
        "technical_plans_missing_or_invalid": 0,
        "llm_requests_unmatched": 0,
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
    plans_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in plans.to_dict("records"):
        plans_by_event.setdefault(str(row["event_id"]), []).append(row)

    population_rows = []
    for opportunity in opportunities.to_dict("records"):
        event_id = str(opportunity["event_id"])
        technical = json.loads(opportunity["snapshot_json"])
        technical_decision = str(technical.get("technical_decision", technical.get("decision", ""))).upper()
        technical_evaluation = technical.get("technical_evaluation") or {}
        all_context_rows = contexts_by_event.get(event_id, [])
        context_rows = [row for row in all_context_rows if row.get("context_type") == "TRADE_PLAN_RISK_INPUT"]
        trade_context = {}
        if len(context_rows) == 1:
            try:
                trade_context = json.loads(context_rows[0]["context_json"])
            except (TypeError, json.JSONDecodeError):
                trade_context = {}
        plan_rows = [row for row in plans_by_event.get(event_id, [])
                     if row.get("policy_version") == "TECHNICAL_REFERENCE_V1"]
        technical_plan: dict[str, Any] = {}
        if len(plan_rows) == 1:
            try:
                technical_plan = dict(plan_rows[0])
                technical_plan["plan"] = json.loads(technical_plan.get("plan_json") or "null")
                technical_plan["market_data_quality"] = json.loads(technical_plan.get("market_data_quality_json") or "{}")
                technical_plan["evidence_ids"] = json.loads(technical_plan.get("evidence_ids_json") or "[]")
                technical_plan["missing_fields"] = json.loads(technical_plan.get("missing_fields_json") or "[]")
                if technical_plan.get("plan") is not None and canonical_hash(technical_plan["plan"]) != technical_plan.get("plan_sha256"):
                    raise ValueError("TECHNICAL_PLAN_HASH_MISMATCH")
            except (TypeError, ValueError, json.JSONDecodeError):
                technical_plan = {}
        if technical_decision in {"WATCH_LONG", "WATCH_SHORT"} and (
            len(plan_rows) != 1 or not technical_plan
            or technical_plan.get("plan_source") != "TECHNICAL_ONLY_REFERENCE"
            or technical_plan.get("policy_sha256") != TECHNICAL_REFERENCE_POLICY_SHA256
            or technical_plan.get("plan_status") not in {"TECHNICAL_PLAN_VALID", "TECHNICAL_PLAN_NOT_EVALUABLE"}
        ):
            counts["technical_plans_missing_or_invalid"] += 1
        outcome_rows = outcomes[outcomes["event_id"].astype(str).eq(event_id)] if len(outcomes) else pd.DataFrame()
        event_outcomes = []
        decision_timestamp = utc_timestamp(opportunity["decision_time_utc"], field="decision_time")
        for outcome_row in outcome_rows.to_dict("records"):
            try:
                record = json.loads(outcome_row["outcome_json"])
                release = record.get("economic_close_time_utc") or record.get("release_time_utc")
                observed = record.get("outcome_observed_at_utc") or record.get("known_at_utc") or outcome_row["known_at_utc"]
                persisted = record.get("persisted_at_utc") or outcome_row["known_at_utc"]
                close_time = utc_timestamp(release, field="economic_close_time") if release else None
                observed_time = utc_timestamp(observed, field="outcome_observed_at")
                persisted_time = utc_timestamp(persisted, field="outcome_persisted_at")
                fill_time = utc_timestamp(record["fill_time_utc"], field="fill_time") if record.get("fill_time_utc") else None
                plan_matches = (
                    record.get("policy_id") != "COMMON_PLAN_COUNTERFACTUAL"
                    or record.get("plan_sha256") == technical_plan.get("plan_sha256")
                )
                chronology_ok = (
                    observed_time >= decision_timestamp
                    and (close_time is None or close_time >= decision_timestamp)
                    and (close_time is None or observed_time >= close_time)
                    and (fill_time is None or close_time is None or close_time >= fill_time)
                    and persisted_time >= observed_time
                )
                if not chronology_ok or not plan_matches:
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
            "plan": technical_plan.get("plan"),
            "plan_sha256": technical_plan.get("plan_sha256"),
            "technical_plan_status": technical_plan.get("plan_status", "MISSING_OR_INVALID"),
            "technical_plan_version": technical_plan.get("policy_version"),
            "technical_plan_policy_sha256": technical_plan.get("policy_sha256"),
            "technical_plan_payload_sha256": technical_plan.get("payload_sha256"),
            "technical_plan_created_at_utc": technical_plan.get("constructed_at_utc"),
            "technical_plan_max_market_data_time_utc": technical_plan.get("maximum_market_data_time_utc"),
            "technical_plan_data_quality": technical_plan.get("market_data_quality", {}),
            "technical_plan_evidence_ids": technical_plan.get("evidence_ids", []),
            "risk_context": trade_context.get("risk_context"),
            "trade_context_sha256": context_rows[0].get("payload_sha256") if len(context_rows) == 1 else None,
            "plan_strategy_source": technical_plan.get("plan_source"),
            "trade_context_status": "MISSING_RISK_CONTEXT",
            "outcomes": event_outcomes,
        }
        context_times_valid = False
        plan_built = context_known = risk_known = None
        if len(context_rows) == 1 and technical_plan:
            try:
                market_time = utc_timestamp(trade_context["market_data_time_utc"], field="market_data_time")
                context_known = utc_timestamp(trade_context["context_known_at_utc"], field="context_known_at")
                risk_known = utc_timestamp(trade_context["risk_context_known_at_utc"], field="risk_context_known_at")
                plan_built = utc_timestamp(technical_plan["constructed_at_utc"], field="technical_plan_created_at")
                plan_market = utc_timestamp(technical_plan["maximum_market_data_time_utc"], field="technical_plan_market_data_time")
                context_times_valid = (
                    market_time <= decision_timestamp and plan_market <= decision_timestamp
                    and context_known >= market_time and risk_known >= market_time
                    and plan_built >= decision_timestamp
                )
            except (ValueError, TypeError, KeyError):
                context_times_valid = False
        if context_times_valid:
            base["decision_time_utc"] = max(plan_built, context_known, risk_known).isoformat().replace("+00:00", "Z")
            base["trade_context_status"] = "TIMESTAMPS_VERIFIED"
        elif technical_decision in {"WATCH_LONG", "WATCH_SHORT"}:
            counts["trade_contexts_missing_or_invalid"] += 1
            base["risk_context"] = None
            base["trade_context_status"] = "MISSING_OR_FUTURE_DATA_CONTEXT"
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
                "llm_coverage_status": coverage_status, "llm_covered": False, "request_id": None,
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
            sent_requests = [row for row in coverage_rows
                             if row.get("event_type") == "LLM_QUERY_SENT"
                             and row.get("agent_role") == CANONICAL_FILTER_ROLE]
            persisted_responses = [row for row in coverage_rows
                                   if row.get("event_type") == "LLM_RESPONSE_PERSISTED"
                                   and row.get("agent_role") == CANONICAL_FILTER_ROLE]
            provenance_ok = (
                event["provider"] == CANONICAL_PROVIDER
                and event["configured_model"] == CANONICAL_MODEL
                and event.get("reported_model") == CANONICAL_MODEL
                and event.get("decision_policy_id") == POLICY_VERSION
                and event["prompt_sha256"] == canonical_prompt_sha256()
                and len(event["payload_sha256"]) == 64
                and request >= decision_timestamp and response >= request
                and event["pydantic_status"] == "VALID"
                and bool(event.get("request_id"))
                and len(sent_requests) == 1 and sent_requests[0].get("request_id") == event.get("request_id")
                and sent_requests[0].get("payload_sha256") == event["payload_sha256"]
                and len(persisted_responses) == 1 and persisted_responses[0].get("request_id") == event.get("request_id")
                and persisted_responses[0].get("payload_sha256") == event["payload_sha256"]
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
                "llm_coverage_status": "CANONICAL_RESPONSE_INVALID_OR_UNVERIFIED", "request_id": event.get("request_id"),
                "llm_covered": False, "decision_match": None})
            continue

        system_rows = systems_by_event.get(event_id, [])
        system_valid = False
        if len(system_rows) == 1:
            try:
                system_valid = (
                    system_rows[0]["validation_status"] == "VALID"
                    and bool(event.get("request_id"))
                    and system_rows[0].get("request_id") == event.get("request_id")
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
                "request_id": event.get("request_id"),
                "decision_match": None})
            continue

        # The source candle must not be newer than the opportunity. Market
        # metadata and plan inputs must be timestamped and known before the
        # common simulated decision time.
        if context_times_valid:
            effective_decision = max(
                response, utc_timestamp(system_rows[0]["created_at_utc"], field="system_decision_time"),
                utc_timestamp(base["decision_time_utc"], field="risk_context_effective_time"),
            )
            base["decision_time_utc"] = effective_decision.isoformat().replace("+00:00", "Z")
        else:
            base["risk_context"] = None
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
            "request_id": event.get("request_id"),
            "llm_validation": event["pydantic_status"],
            "llm_coverage_status": "VALID_CANONICAL_RESPONSE" if final_consistent else "SYSTEM_DECISION_MISMATCH",
            "llm_covered": bool(final_consistent), "decision_match": bool(same)})
    return pd.DataFrame(population_rows), counts


def pair_ledger(path: str | Path) -> dict[str, Any]:
    opportunities, llm, systems, outcomes, coverage, contexts, plans = load_pairing_tables(path)
    population, counts = _event_checks(opportunities, llm, systems, outcomes, coverage, contexts, plans)
    paired = population.loc[population["llm_covered"]] if len(population) else population
    report = {
        "schema_version": "2.1",
        "status": "PAIRING_COVERAGE_AVAILABLE" if len(paired) else "NO_VERIFIABLE_PAIRS",
        "counts": counts,
        "pairs": paired.to_dict("records"),
        "population": population.to_dict("records"),
        "system_events_total": int(len(systems)),
        "coverage_events_total": int(len(coverage)),
        "trade_contexts_total": int(len(contexts)),
        "technical_plans_total": int(len(plans)),
        "source_ledger_path": str(Path(path).resolve()),
        "source_ledger_sha256": ledger_content_sha256(path),
        "reconciliation": reconcile_ledger(path),
        "limitations": [
            "Only events carrying valid provider, model, prompt, payload, evidence and UTC provenance are eligible.",
            "Every response is joined by unique request_id; duplicate or absent request identifiers are not paired.",
            "No missing historical model decisions are synthesized.",
            "Both policies use the single append-only TECHNICAL_REFERENCE_V1 plan attached to each opportunity.",
            "Economic comparison requires causal outcomes, complete risk context and a PASS reconciliation.",
        ],
    }
    report["pairing_snapshot_sha256"] = canonical_hash({
        key: report[key] for key in (
            "schema_version", "counts", "population", "system_events_total", "coverage_events_total",
            "trade_contexts_total", "technical_plans_total", "source_ledger_sha256", "reconciliation",
        )
    })
    return report


def compare_paired_policies(paired_events: list[dict[str, Any]] | dict[str, Any], *, cost_scenario: str = "CONSERVATIVE",
                            max_open_positions: int | None = None, initial_equity_eur: float = 10_000.0,
                            state_dir: str | Path | None = None,
                            ledger_path: str | Path | None = None) -> dict[str, Any]:
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
            "reason": "Pass a pair_ledger report and the source SQLite ledger path.",
            "economic_inference_allowed": False,
        }
    if ledger_path is None:
        return {
            "status": "NO_EVALUABLE_SOURCE_LEDGER_REQUIRED",
            "reason": "A report status field is not proof of reconciliation; the source ledger must be revalidated.",
            "economic_inference_allowed": False,
        }
    try:
        current = pair_ledger(ledger_path)
    except Exception:
        return {"status": "NO_EVALUABLE_SOURCE_LEDGER_INVALID", "economic_inference_allowed": False}
    if (paired_events.get("source_ledger_sha256") != current.get("source_ledger_sha256")
            or paired_events.get("pairing_snapshot_sha256") != current.get("pairing_snapshot_sha256")):
        return {"status": "NO_EVALUABLE_SOURCE_OR_REPORT_MISMATCH", "economic_inference_allowed": False}
    reconciliation = current.get("reconciliation") or {}
    if reconciliation.get("status") != "PASS":
        return {
            "status": "NO_EVALUABLE_RECONCILIATION_FAILED",
            "reconciliation": reconciliation,
            "economic_inference_allowed": False,
        }
    population = current.get("population")
    counts = current.get("counts")
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
    result = simulate_paired_policies(
        normalized_events, cost_scenario=cost_scenario,
        initial_equity_eur=initial_equity_eur, state_dir=state_dir,
    )
    result["source_ledger_sha256"] = current["source_ledger_sha256"]
    result["pairing_snapshot_sha256"] = current["pairing_snapshot_sha256"]
    result["reconciliation"] = reconciliation
    return result
