"""Offline provenance checks and same-opportunity event pairing."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd

from src.phase2_1_evaluation import utc_timestamp


def load_pairing_tables(path: str | Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    db = sqlite3.connect(str(path))
    try:
        opportunities = pd.read_sql_query("SELECT * FROM opportunities", db)
        llm = pd.read_sql_query("SELECT * FROM llm_events", db)
        systems = pd.read_sql_query("SELECT * FROM system_events", db)
        outcomes = pd.read_sql_query("SELECT * FROM outcomes", db)
    finally:
        db.close()
    return opportunities, llm, systems, outcomes


def _event_checks(opportunities: pd.DataFrame, llm: pd.DataFrame, systems: pd.DataFrame, outcomes: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    counts = {
        "technical_events_total": int(len(opportunities)),
        "llm_events_total": int(len(llm)),
        "llm_valid": int(llm["pydantic_status"].eq("VALID").sum()) if len(llm) else 0,
        "llm_invalid": int(llm["pydantic_status"].ne("VALID").sum()) if len(llm) else 0,
        "hold": 0,
        "buy_sell": 0,
        "matching_decisions": 0,
        "disagreeing_decisions": 0,
        "events_without_llm_coverage": 0,
        "duplicate_event_ids": 0,
        "unverified_provenance": 0,
        "outcome_events": int(outcomes["event_id"].nunique()) if len(outcomes) else 0,
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
        if row.get("decision_stage") == "MARKET_ANALYSIS":
            systems_by_event.setdefault(str(row["event_id"]), []).append(row)

    population_rows = []
    for opportunity in opportunities.to_dict("records"):
        event_id = str(opportunity["event_id"])
        technical = json.loads(opportunity["snapshot_json"])
        technical_decision = str(technical.get("technical_decision", technical.get("decision", ""))).upper()
        outcome_rows = outcomes[outcomes["event_id"].astype(str).eq(event_id)] if len(outcomes) else pd.DataFrame()
        event_outcomes = []
        decision_timestamp = utc_timestamp(opportunity["decision_time_utc"], field="decision_time")
        for outcome_row in outcome_rows.to_dict("records"):
            try:
                known_at = utc_timestamp(outcome_row["known_at_utc"], field="known_at")
                outcome_record = json.loads(outcome_row["outcome_json"])
                if known_at < decision_timestamp:
                    counts["unverified_provenance"] += 1
                    continue
                event_outcomes.append(outcome_record)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                counts["unverified_provenance"] += 1
        events = llm_by_event.get(event_id, [])
        eligible = []
        for event in events:
            try:
                request = utc_timestamp(event["requested_at_utc"], field="requested_at")
                response = utc_timestamp(event["responded_at_utc"], field="responded_at")
                decision_time = decision_timestamp
                allowed = set(json.loads(event["allowed_evidence_json"]))
                decision = json.loads(event["decision_json"] or "{}")
                used = decision.get("evidence_used", decision.get("evidence_ids", []))
                provenance_ok = (
                    event["provider"] in {"deepseek", "ollama"}
                    and bool(event["configured_model"])
                    and len(event["prompt_sha256"]) == 64
                    and len(event["payload_sha256"]) == 64
                    and request >= decision_time
                    and response >= request
                    and event["pydantic_status"] == "VALID"
                    and (not used or isinstance(used, list) and all(item in allowed for item in used))
                )
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                provenance_ok = False
                decision = {}
            if provenance_ok:
                eligible.append((event, decision))
            else:
                counts["unverified_provenance"] += 1
        if not eligible:
            counts["events_without_llm_coverage"] += 1
            unverified = bool(events)
            population_rows.append({
                "event_id": event_id, "symbol": opportunity["symbol"],
                "decision_time_utc": opportunity["decision_time_utc"],
                "technical_decision": technical_decision or None,
                "technical_setup_quality": (technical.get("technical_evaluation") or {}).get("setup_quality", technical.get("setup_quality")),
                "technical_overall_score": (technical.get("technical_evaluation") or {}).get("overall_score", technical.get("overall_score")),
                "llm_decision": None, "llm_model_output": None,
                "provider": None, "model": None, "prompt_sha256": None, "payload_sha256": None,
                "llm_validation": "INVALID" if unverified else "NO_COVERAGE",
                "llm_covered": False, "decision_match": None, "outcomes": event_outcomes,
            })
            continue
        # Multiple agent calls do not define a single DeepSeek vote. Keep these
        # events visible as ambiguous coverage instead of choosing one silently.
        if len(eligible) != 1:
            counts["duplicate_event_ids"] += 1
            counts["events_without_llm_coverage"] += 1
            population_rows.append({
                "event_id": event_id, "symbol": opportunity["symbol"],
                "decision_time_utc": opportunity["decision_time_utc"],
                "technical_decision": technical_decision or None,
                "technical_setup_quality": (technical.get("technical_evaluation") or {}).get("setup_quality", technical.get("setup_quality")),
                "technical_overall_score": (technical.get("technical_evaluation") or {}).get("overall_score", technical.get("overall_score")),
                "llm_decision": None, "llm_model_output": None,
                "provider": None, "model": None, "prompt_sha256": None, "payload_sha256": None,
                "llm_validation": "AMBIGUOUS_MULTIPLE_CALLS", "llm_covered": False,
                "decision_match": None, "outcomes": event_outcomes,
            })
            continue
        event, decision = eligible[0]
        system_decisions = systems_by_event.get(event_id, [])
        system_timestamp_valid = False
        if len(system_decisions) == 1:
            try:
                system_timestamp_valid = utc_timestamp(system_decisions[0]["created_at_utc"], field="system_decision_time") >= utc_timestamp(event["responded_at_utc"], field="responded_at")
            except (ValueError, TypeError, KeyError):
                system_timestamp_valid = False
        if len(system_decisions) != 1 or system_decisions[0].get("validation_status") != "VALID" or not system_timestamp_valid:
            counts["events_without_llm_coverage"] += 1
            population_rows.append({
                "event_id": event_id, "symbol": opportunity["symbol"],
                "decision_time_utc": opportunity["decision_time_utc"],
                "technical_decision": technical_decision or None,
                "technical_setup_quality": (technical.get("technical_evaluation") or {}).get("setup_quality", technical.get("setup_quality")),
                "technical_overall_score": (technical.get("technical_evaluation") or {}).get("overall_score", technical.get("overall_score")),
                "llm_decision": None, "llm_model_output": None,
                "provider": event["provider"], "model": event["configured_model"],
                "prompt_sha256": event["prompt_sha256"], "payload_sha256": event["payload_sha256"],
                "llm_validation": "NO_VALID_SYSTEM_DECISION", "llm_covered": False,
                "decision_match": None, "outcomes": event_outcomes,
            })
            continue
        llm_model_output = str(decision.get("decision", decision.get("action", decision.get("direction", decision.get("strategy", ""))))).upper()
        llm_decision = str(system_decisions[0]["final_decision"]).upper()
        decision_actions = {"BUY", "SELL", "HOLD", "WAIT", "WATCH_LONG", "WATCH_SHORT", "NO_TRADE"}
        if technical_decision not in decision_actions or llm_decision not in decision_actions:
            counts["events_without_llm_coverage"] += 1
            population_rows.append({
                "event_id": event_id, "symbol": opportunity["symbol"],
                "decision_time_utc": opportunity["decision_time_utc"],
                "technical_decision": technical_decision or None,
                "technical_setup_quality": (technical.get("technical_evaluation") or {}).get("setup_quality", technical.get("setup_quality")),
                "technical_overall_score": (technical.get("technical_evaluation") or {}).get("overall_score", technical.get("overall_score")),
                "llm_decision": llm_decision or None, "llm_model_output": llm_model_output,
                "provider": event["provider"], "model": event["configured_model"],
                "prompt_sha256": event["prompt_sha256"], "payload_sha256": event["payload_sha256"],
                "llm_validation": "DECISION_NOT_COMPARABLE", "llm_covered": False,
                "decision_match": None, "outcomes": event_outcomes,
            })
            continue
        counts["hold"] += int(llm_decision in {"HOLD", "WAIT", "NO_TRADE"})
        counts["buy_sell"] += int(llm_decision in {"BUY", "SELL", "WATCH_LONG", "WATCH_SHORT"})
        same = technical_decision and llm_decision and technical_decision == llm_decision
        if same:
            counts["matching_decisions"] += 1
        elif technical_decision and llm_decision:
            counts["disagreeing_decisions"] += 1
        population_rows.append({
            "event_id": event_id,
            "symbol": opportunity["symbol"],
            "decision_time_utc": opportunity["decision_time_utc"],
            "technical_decision": technical_decision or None,
            "technical_setup_quality": (technical.get("technical_evaluation") or {}).get("setup_quality", technical.get("setup_quality")),
            "technical_overall_score": (technical.get("technical_evaluation") or {}).get("overall_score", technical.get("overall_score")),
            "llm_decision": llm_decision or None,
            "llm_model_output": llm_model_output,
            "provider": event["provider"],
            "model": event["configured_model"],
            "prompt_sha256": event["prompt_sha256"],
            "payload_sha256": event["payload_sha256"],
            "llm_validation": event["pydantic_status"],
            "llm_covered": True,
            "decision_match": bool(same),
            "outcomes": event_outcomes,
        })
    return pd.DataFrame(population_rows), counts


def pair_ledger(path: str | Path) -> dict[str, Any]:
    opportunities, llm, systems, outcomes = load_pairing_tables(path)
    population, counts = _event_checks(opportunities, llm, systems, outcomes)
    paired = population.loc[population["llm_covered"]] if len(population) else population
    return {
        "schema_version": "1.0",
        "status": "READY_FOR_PROSPECTIVE_DATA" if len(paired) else "NO_VERIFIABLE_PAIRS",
        "counts": counts,
        "pairs": paired.to_dict("records"),
        "population": population.to_dict("records"),
        "system_events_total": int(len(systems)),
        "limitations": [
            "Only events carrying valid provider, model, prompt, payload, evidence and UTC provenance are eligible.",
            "Multiple valid LLM calls for one opportunity are excluded until a preregistered decision role is supplied.",
            "No missing historical model decisions are synthesized.",
            "Economic comparison requires independently recorded policy outcomes for both policies and portfolio constraints.",
        ],
    }


def compare_paired_policies(paired_events: list[dict[str, Any]], *, cost_scenario: str = "BASE", max_open_positions: int = 3) -> dict[str, Any]:
    """Offline portfolio-aware comparator for common paired outcomes.

    Each event must contain a prospectively recorded `TRADE_CANDIDATE` outcome
    from the same frozen technical plan and cost scenario. Outcomes remain
    future labels; policy inclusion uses only contemporaneous decisions.
    """
    if max_open_positions < 1:
        raise ValueError("MAX_OPEN_POSITIONS_INVALID")
    records = []
    for event in paired_events:
        outcome = next((item for item in event.get("outcomes", []) if item.get("policy_id") == "TRADE_CANDIDATE" and item.get("cost_scenario") == cost_scenario), None)
        records.append((event, outcome))
    if not records or not any(outcome is not None for _, outcome in records):
        return {"status": "NOT_EVALUABLE_INSUFFICIENT_OUTCOMES", "cost_scenario": cost_scenario, "policies": {name: {"opportunities": len(records), "accepted": 0, "rejected_portfolio": 0, "closed": 0, "net_pnl_eur": 0.0} for name in ("TECHNICAL_ONLY", "TECHNICAL_PLUS_LLM", "ALWAYS_HOLD")}}

    actions = {"BUY", "SELL", "WATCH_LONG", "WATCH_SHORT"}
    output = {}
    for policy in ("TECHNICAL_ONLY", "TECHNICAL_PLUS_LLM", "ALWAYS_HOLD"):
        active: list[tuple[str, pd.Timestamp]] = []
        metrics = {"opportunities": len(records), "eligible": 0, "accepted": 0, "rejected_portfolio": 0, "closed": 0, "ambiguous": 0, "open_or_unresolved": 0, "net_pnl_eur": 0.0, "net_r_sum": 0.0}
        ordered = sorted(records, key=lambda pair: (
            utc_timestamp(pair[0]["decision_time_utc"]),
            -float(pair[0].get("technical_setup_quality") or 0),
            -abs(float(pair[0].get("technical_overall_score") or 0)),
            str(pair[0].get("symbol", "")),
        ))
        for event, outcome in ordered:
            decision_time = utc_timestamp(event["decision_time_utc"])
            active = [(symbol, release) for symbol, release in active if release > decision_time]
            technical = str(event.get("technical_decision", "")).upper()
            llm = str(event.get("llm_decision", "")).upper()
            eligible = technical in actions and (policy != "TECHNICAL_PLUS_LLM" or llm == technical)
            if policy == "ALWAYS_HOLD":
                eligible = False
            if not eligible:
                continue
            metrics["eligible"] += 1
            if outcome is None:
                metrics["open_or_unresolved"] += 1
                active.append((str(event["symbol"]), pd.Timestamp.max.tz_localize("UTC")))
                metrics["accepted"] += 1
                continue
            if outcome.get("portfolio_accepted") is False:
                metrics["rejected_portfolio"] += 1
                continue
            if any(symbol == event["symbol"] for symbol, _ in active) or len(active) >= max_open_positions:
                metrics["rejected_portfolio"] += 1
                continue
            release_value = outcome.get("release_time_utc")
            release_time = utc_timestamp(release_value, field="release_time") if release_value else pd.Timestamp.max.tz_localize("UTC")
            active.append((str(event["symbol"]), release_time))
            metrics["accepted"] += 1
            if outcome.get("ambiguous") or str(outcome.get("result", "")).startswith("AMBIGUOUS"):
                metrics["ambiguous"] += 1
                continue
            pnl = outcome.get("net_pnl_eur", outcome.get("net_pnl"))
            if pnl is None or not release_value:
                metrics["open_or_unresolved"] += 1
                continue
            metrics["closed"] += 1
            metrics["net_pnl_eur"] += float(pnl)
            if outcome.get("net_r") is not None:
                metrics["net_r_sum"] += float(outcome["net_r"])
        metrics["net_pnl_eur"] = round(metrics["net_pnl_eur"], 8)
        metrics["net_r_sum"] = round(metrics["net_r_sum"], 8)
        metrics["expectancy_net_eur_per_closed_trade"] = round(metrics["net_pnl_eur"] / metrics["closed"], 8) if metrics["closed"] else None
        output[policy] = metrics
    return {
        "status": "DESCRIPTIVE_PAIRED_POLICY_COMPARISON",
        "cost_scenario": cost_scenario,
        "portfolio_constraints": {"max_open_positions": max_open_positions, "one_active_position_per_symbol": True, "same_timestamp_priority": ["technical_setup_quality_desc", "abs_overall_score_desc", "symbol_asc"]},
        "future_outcomes_used_only_for_scoring": True,
        "policies": output,
        "limitations": ["The comparator is valid only if every candidate outcome was simulated under the exact same plan and cost scenario.", "An inconclusive/no-sample result is not evidence of no LLM effect.", "Production portfolio risk sizing and broker validation remain outside this offline paired interface."],
    }
