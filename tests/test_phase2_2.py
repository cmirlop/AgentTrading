"""Offline tests for Fase 2.2 risk parity, ledger and prospective protocol."""

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from config import risk_config
from src.agents.llm_client import LLMProviderError, structured_chat
from src.daily_risk_guard import evaluate_daily_risk
from src.event_ledger import (
    LedgerPersistenceError, append_outcome, bind_event, export_anonymized_ledger,
    canonical_hash, initialize_ledger, record_coverage_event, record_llm_call,
    record_llm_query_started, record_opportunity, record_system_decision,
    record_technical_plan, record_trade_context, reconcile_ledger,
)
from src.llm_evaluation import compare_paired_policies, pair_ledger
from src.paired_policy_simulator import simulate_paired_policies
from src.research_policy import (
    BudgetGate, BudgetLimits, CANONICAL_FILTER_ROLE, CANONICAL_MODEL,
    CANONICAL_PROVIDER, MARKET_ANALYST_FILTER_PROMPT, POLICY_VERSION,
    canonical_prompt_sha256, deepseek_filter_outcome,
)
from src.risk_engine import calculate_currency_exposure, evaluate_portfolio_limits
from src.risk_preflight import evaluate_plan_risk
from src.technical_reference import POLICY_SHA256 as TECHNICAL_REFERENCE_POLICY_SHA256


class _Decision(BaseModel):
    decision: str
    evidence_used: list[str]


def _risk_context():
    metadata = {
        "EURUSD": {"currency_base": "EUR", "currency_profit": "USD", "trade_contract_size": 100000, "trade_tick_size": .00001, "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR", "volume_min": .01, "volume_max": 100, "volume_step": .01},
        "USDCAD": {"currency_base": "USD", "currency_profit": "CAD", "trade_contract_size": 100000, "trade_tick_size": .00001, "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR", "volume_min": .01, "volume_max": 100, "volume_step": .01},
        "AUDUSD": {"currency_base": "AUD", "currency_profit": "USD", "trade_contract_size": 100000, "trade_tick_size": .00001, "trade_tick_value_loss": 1, "trade_tick_value_loss_currency": "EUR", "volume_min": .01, "volume_max": 100, "volume_step": .01},
    }
    return {"account_currency": "EUR", "symbol_metadata": metadata,
            "conversion_rates": {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67}}


def _event(event_id="e1", *, minute=0, symbol="EURUSD", action="WATCH_LONG", strategy="PULLBACK",
           net=2.0, cost=0.25, decision_time=None, filled=True, release_minutes=30,
           expiry_minutes=None, coverage="VALID_CANONICAL_RESPONSE", risk_context=None,
           entry=None, stop=None, quality=80):
    base = decision_time or datetime(2026, 3, 1, tzinfo=timezone.utc)
    now = base + timedelta(minutes=minute)
    price = entry if entry is not None else {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67}[symbol]
    direction = "SELL" if action in {"WATCH_SHORT", "SELL"} else "BUY"
    distance = stop if stop is not None else .001
    stop_loss = price + distance if direction == "SELL" else price - distance
    take_profit = price - distance * 2 if direction == "SELL" else price + distance * 2
    release = now + timedelta(minutes=release_minutes) if release_minutes is not None else None
    plan = {"symbol": symbol, "direction": direction, "entry": price, "stop_loss": stop_loss,
        "take_profit": take_profit, "order_type": "BUY_LIMIT" if direction == "BUY" else "SELL_LIMIT",
        "mode": "LIMIT", "target_rr": 2.1}
    outcome = {
        "filled": filled, "fill_time_utc": now.isoformat() if filled else None,
        "release_time_utc": release.isoformat() if release and filled else None,
        "economic_close_time_utc": release.isoformat() if release and filled else None,
        "known_at_utc": release.isoformat() if release and filled else None,
        "outcome_observed_at_utc": release.isoformat() if release and filled else None,
        "persisted_at_utc": (release + timedelta(seconds=1)).isoformat() if release and filled else None,
        "accepted_at_utc": now.isoformat(), "placed_at_utc": now.isoformat(),
        "expires_at_utc": (now + timedelta(minutes=expiry_minutes)).isoformat() if expiry_minutes is not None else None,
        "outcome_status": "RESOLVED" if release and filled else "UNRESOLVED",
        "result": "WIN" if net > 0 else "LOSS" if net < 0 else "FLAT",
        "net_pnl_eur": net, "gross_pnl_eur": net + cost, "costs_eur": cost,
        "net_r": net / 5.0, "base_volume": .05, "cost_scenario": "CONSERVATIVE",
        "plan_sha256": canonical_hash(plan),
    }
    return {
        "event_id": event_id, "symbol": symbol, "decision_time_utc": now.isoformat(),
        "technical_decision": action, "technical_setup_quality": quality,
        "technical_overall_score": quality / 2,
        "llm_decision": action if strategy != "NONE" else "NO_TRADE",
        "llm_model_output": strategy, "llm_role": CANONICAL_FILTER_ROLE,
        "decision_policy_id": POLICY_VERSION, "provider": CANONICAL_PROVIDER,
        "model": CANONICAL_MODEL, "prompt_sha256": canonical_prompt_sha256(),
        "llm_validation": "VALID", "llm_covered": True,
        "llm_coverage_status": coverage,
        "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE",
        "risk_context": risk_context or _risk_context(),
        "technical_plan_status": "TECHNICAL_PLAN_VALID",
        "technical_plan_version": "TECHNICAL_REFERENCE_V1",
        "technical_plan_policy_sha256": TECHNICAL_REFERENCE_POLICY_SHA256,
        "plan": plan, "plan_sha256": canonical_hash(plan),
        "outcome": outcome,
    }


def _record_reference_plan(event_id, decision_time, *, plan=None, status="TECHNICAL_PLAN_VALID", path):
    decision = datetime.fromisoformat(str(decision_time).replace("Z", "+00:00")).astimezone(timezone.utc)
    result = {
        "schema_version": "1.0", "policy_version": "TECHNICAL_REFERENCE_V1",
        "policy_sha256": TECHNICAL_REFERENCE_POLICY_SHA256,
        "plan_source": "TECHNICAL_ONLY_REFERENCE", "status": status,
        "symbol": (plan or {}).get("symbol", "EURUSD"),
        "decision_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "constructed_at_utc": (decision + timedelta(seconds=5)).isoformat().replace("+00:00", "Z"),
        "maximum_market_data_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "technical_decision": "WATCH_LONG" if plan else "NO_TRADE",
        "direction": (plan or {}).get("direction"), "plan": plan,
        "plan_sha256": canonical_hash(plan) if plan else None,
        "reason": "SYNTHETIC_TEST_PLAN", "missing_fields": [],
        "spread_quality": "OBSERVED_VALID",
        "confirmatory_eligible": bool(plan),
        "technical_snapshot": {"bias": "BULLISH", "alignment": "ALIGNED",
                               "setup_quality": 80, "overall_score": 40,
                               "available_evidence_ids": ["E1"]},
    }
    return record_technical_plan(event_id, result, path=path)


def _write_concurrent_call(args):
    db_path, event_id, number = args
    with bind_event(event_id, db_path, agent_role="TEST_DIAGNOSTIC", agent_stage="TEST"):
        record_llm_query_started(role="TEST_DIAGNOSTIC", policy_id=None, payload={"id": number})
        return record_llm_call(
            schema_name="SyntheticDiagnostic", provider="mock", model="offline",
            prompt="mock", payload={"id": number}, allowed_evidence=[],
            requested_at="2026-01-01T00:00:01Z",
            result=type("R", (), {"model_dump": lambda self, **kwargs: {"decision": "WAIT"}})(),
            agent_role="TEST_DIAGNOSTIC", agent_stage="TEST",
        ).status


class Phase22Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "research.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_risk_config_is_source_of_two_slot_limit_and_override_is_rejected(self):
        self.assertEqual(risk_config.MAX_OPEN_POSITIONS, 2)
        self.assertEqual(compare_paired_policies([], max_open_positions=3)["status"], "NO_EVALUABLE_RISK_CONFIG_OVERRIDE")

    def test_explicit_taxonomy_does_not_turn_watch_into_buy_sell(self):
        self.assertEqual(canonical_prompt_sha256(), "4e54929356d993a6b633904d28c73dc1a2b70b591ee7498906dc0b44dfe198a3")
        self.assertEqual(deepseek_filter_outcome("WATCH_LONG", "PULLBACK", role=CANONICAL_FILTER_ROLE), (True, "MODEL_APPROVED_TECHNICAL_DIRECTION_UNCHANGED"))
        self.assertEqual(deepseek_filter_outcome("WATCH_SHORT", "NONE", role=CANONICAL_FILTER_ROLE)[0], False)
        self.assertEqual(deepseek_filter_outcome("WATCH_LONG", "SELL", role=CANONICAL_FILTER_ROLE)[1], "MODEL_STRATEGY_INVALID")
        self.assertEqual(deepseek_filter_outcome("WATCH_LONG", "PULLBACK", role="CRITIC_VETO")[1], "LLM_ROLE_NOT_CANONICAL")

    def test_budget_mock_blocks_unknown_prices_limits_and_retries(self):
        gate = BudgetGate(BudgetLimits(2, 100, 250, 1, 1))
        blocked = gate.authorize(input_tokens=10, max_output_tokens=50)
        self.assertEqual(blocked["reason"], "VERIFIED_API_PRICES_REQUIRED")
        first = gate.authorize(input_tokens=10, max_output_tokens=50, price_input_per_million=1, price_output_per_million=2, prices_verified=True)
        self.assertTrue(first["authorized"])
        gate.record_usage(request_id=first["request_id"], input_tokens=10, output_tokens=30)
        second = gate.authorize(input_tokens=10, max_output_tokens=50, price_input_per_million=1, price_output_per_million=2, prices_verified=True)
        self.assertTrue(second["authorized"])
        blocked = gate.authorize(input_tokens=1, max_output_tokens=1, price_input_per_million=1, price_output_per_million=1, prices_verified=True)
        self.assertEqual(blocked["reason"], "MAX_REQUESTS_REACHED")
        self.assertTrue(gate.disabled)
        self.assertTrue(all(event.get("real_provider_call") is False for event in gate.events))

    def test_research_strict_without_event_stops_before_provider_call(self):
        response = {"done_reason": "stop", "message": {"content": json.dumps({"decision": "BUY", "evidence_used": ["E1"]})}}
        class FakeOllama:
            calls = 0
            def chat(self, **kwargs):
                self.calls += 1
                return response
        fake = FakeOllama()
        with patch("src.event_ledger.default_ledger_path", return_value=self.db), \
             patch.dict("os.environ", {"RESEARCH_LOG_REQUIRED": "1", "LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "mock"}), \
             patch("src.agents.llm_client._ollama_client", return_value=fake):
            with self.assertRaises(LLMProviderError):
                structured_chat(_Decision, "prompt", {"allowed_evidence_ids": ["E1"]})
        self.assertEqual(fake.calls, 0)
        self.assertGreater(reconcile_ledger(self.db)["counts"]["events_without_traceability"], 0)

    def test_strict_post_response_persistence_failure_is_not_retried(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db)
        class FakeOllama:
            calls = 0
            def chat(self, **kwargs):
                self.calls += 1
                return {"model": "mock", "done_reason": "stop", "message": {"content": json.dumps({"decision": "BUY", "evidence_used": ["E1"]})}}
        fake = FakeOllama()
        from src.event_ledger import LedgerWriteResult
        with patch.dict("os.environ", {"RESEARCH_LOG_REQUIRED": "1", "LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "mock"}), \
             patch("src.agents.llm_client._ollama_client", return_value=fake), \
             patch("src.event_ledger.record_llm_call", return_value=LedgerWriteResult("WRITE_FAILED", False, False, event_id, "LEDGER_SQLITE_BUSY")):
            with bind_event(event_id, self.db, agent_role="TEST_FILTER"):
                with self.assertRaisesRegex(LLMProviderError, "LLM_RESEARCH_LOG_PERSISTENCE_FAILED"):
                    structured_chat(_Decision, "prompt", {"allowed_evidence_ids": ["E1"]})
        self.assertEqual(fake.calls, 1)

    def test_sqlite_failure_is_structured_and_strict_mode_raises(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db)
        with patch("src.event_ledger._connect", side_effect=sqlite3.OperationalError("database is locked")):
            with bind_event(event_id, self.db):
                result = record_llm_call(schema_name="X", provider="deepseek", model="deepseek-flash", prompt="p", payload={}, allowed_evidence=[], requested_at="2026-01-01T00:00:01Z")
        self.assertEqual(result.error_code, "LEDGER_SQLITE_BUSY")
        with patch.dict("os.environ", {"RESEARCH_LOG_REQUIRED": "1"}), patch("src.event_ledger._connect", side_effect=sqlite3.OperationalError("database is locked")):
            with bind_event(event_id, self.db):
                with self.assertRaisesRegex(LedgerPersistenceError, "LEDGER_SQLITE_BUSY"):
                    record_llm_call(schema_name="X", provider="deepseek", model="deepseek-flash", prompt="p", payload={}, allowed_evidence=[], requested_at="2026-01-01T00:00:01Z")

    def test_query_without_persisted_response_fails_reconciliation(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db)
        with bind_event(event_id, self.db, agent_role=CANONICAL_FILTER_ROLE, decision_policy_id=POLICY_VERSION):
            record_llm_query_started(role=CANONICAL_FILTER_ROLE, policy_id=POLICY_VERSION, payload={"test": 1})
        report = reconcile_ledger(self.db)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["counts"]["sent_queries_without_response"], 1)

    def test_ledger_reconciles_all_events_and_outcome_idempotently(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, eligible=True, path=self.db)
        plan = {"symbol": "EURUSD", "direction": "BUY", "order_type": "BUY_LIMIT", "entry": 1.1,
                "stop_loss": 1.099, "take_profit": 1.102, "mode": "LIMIT", "target_rr": 2.1}
        self.assertTrue(_record_reference_plan(event_id, "2026-01-01T00:00:00Z", plan=plan, path=self.db).persisted)
        record_trade_context(event_id, {"candidate_status": "ELIGIBLE", "plan_status": "VALID",
            "plan": plan, "risk_context": _risk_context(),
            "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE",
            "market_data_time_utc": "2026-01-01T00:00:00Z",
            "context_known_at_utc": "2026-01-01T00:00:10Z",
            "risk_context_known_at_utc": "2026-01-01T00:00:10Z"}, path=self.db)
        payload = {"allowed_evidence_ids": ["E1"]}
        model_result = type("R", (), {"model_dump": lambda self, **kwargs: {"strategy": "PULLBACK", "evidence_used": ["E1"]}})()
        with bind_event(event_id, self.db, agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION):
            record_llm_query_started(role=CANONICAL_FILTER_ROLE, policy_id=POLICY_VERSION, payload=payload)
            saved = record_llm_call(schema_name="StrategyAssessment", provider=CANONICAL_PROVIDER, model=CANONICAL_MODEL,
                prompt=MARKET_ANALYST_FILTER_PROMPT, payload=payload, allowed_evidence=["E1"],
                requested_at="2026-01-01T00:00:01Z", result=model_result,
                metadata={"schema_module": "src.agents.market_analyst"}, agent_role=CANONICAL_FILTER_ROLE,
                agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION)
            record_system_decision(event_id, "WATCH_LONG", validation_status="VALID", decision_stage="MARKET_ANALYSIS", agent_role=CANONICAL_FILTER_ROLE, decision_policy_id=POLICY_VERSION, path=self.db)
        self.assertTrue(saved.persisted)
        outcome = {"policy_id": "COMMON_PLAN_COUNTERFACTUAL", "cost_scenario": "CONSERVATIVE", "result": "WIN",
                   "known_at_utc": "2026-01-01T00:30:00Z", "outcome_observed_at_utc": "2026-01-01T00:30:00Z",
                   "persisted_at_utc": "2026-01-01T00:30:01Z", "release_time_utc": "2026-01-01T00:30:00Z",
                   "economic_close_time_utc": "2026-01-01T00:30:00Z", "plan_sha256": canonical_hash(plan),
                   "net_pnl_eur": 1, "gross_pnl_eur": 1.25, "costs_eur": .25}
        one = append_outcome(event_id, "2026-01-01T00:30:00Z", outcome, path=self.db)
        two = append_outcome(event_id, "2026-01-01T00:30:00Z", outcome, path=self.db)
        self.assertEqual(one, two)
        report = reconcile_ledger(self.db)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["counts"]["responses_persisted"], 1)
        self.assertEqual(report["counts"]["outcomes_persisted"], 1)
        exported = export_anonymized_ledger(self.db)
        self.assertEqual(len(exported), 1)
        self.assertNotIn("account_number", json.dumps(exported))

    def test_trade_context_is_allowlisted_and_never_overwritten(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db)
        context = {"candidate_status": "ELIGIBLE", "plan_status": "VALID",
            "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE",
            "market_data_time_utc": "2026-01-01T00:00:00Z",
            "context_known_at_utc": "2026-01-01T00:00:01Z",
            "risk_context_known_at_utc": "2026-01-01T00:00:01Z",
            "plan": {"symbol": "EURUSD", "direction": "BUY", "entry": 1.1, "stop_loss": 1.09,
                     "take_profit": 1.12, "volume": 99, "account_balance": 100000},
            "risk_context": {**_risk_context(), "equity": 100000, "account_number": "secret",
                             "positions": [{"symbol": "EURUSD", "volume": 4}]}}
        first = record_trade_context(event_id, context, path=self.db)
        self.assertEqual(first.status, "SAVED")
        self.assertEqual(record_trade_context(event_id, context, path=self.db).status, "DUPLICATE_IDENTICAL")
        changed = {**context, "plan": {**context["plan"], "entry": 1.2}}
        self.assertEqual(record_trade_context(event_id, changed, path=self.db).status, "WRITE_FAILED")
        with sqlite3.connect(self.db) as db:
            encoded = db.execute("SELECT context_json FROM trade_contexts WHERE event_id=?", (event_id,)).fetchone()[0]
        data = json.loads(encoded)
        self.assertEqual(data["plan"]["entry"], 1.1)
        self.assertNotIn("volume", data["plan"])
        self.assertNotIn("equity", data["risk_context"])
        self.assertNotIn("account_number", encoded)
        self.assertNotIn("positions", encoded)

    def test_reconciliation_fails_on_orphan_coverage_and_missing_plan_context(self):
        record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, eligible=True, path=self.db)
        record_coverage_event("LLM_QUERY_SENT", payload={"x": 1}, path=self.db)
        report = reconcile_ledger(self.db)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("ORPHAN_COVERAGE_EVENTS", report["discrepancies"])
        self.assertIn("ELIGIBLE_OPPORTUNITY_WITHOUT_TRADE_CONTEXT", report["discrepancies"])

    def test_real_simulation_rejects_model_selected_counterfactual_plan(self):
        event = _event("model-plan", decision_time=datetime(2026, 3, 1, tzinfo=timezone.utc))
        event["plan_strategy_source"] = "MODEL_SELECTED_STRATEGY_FAMILY"
        report = simulate_paired_policies([event])
        self.assertEqual(report["status"], "NO_EVALUABLE")
        self.assertTrue(any(row["reason"] == "COUNTERFACTUAL_PLAN_NOT_INDEPENDENT_OF_LLM"
                            for row in report["incomplete_events"]))

    def test_pairer_rejects_context_from_a_candle_after_the_opportunity(self):
        opportunity_time = "2026-01-01T00:00:00Z"
        event_id = record_opportunity("EURUSD", opportunity_time,
            {"technical_decision": "WATCH_LONG", "technical_evaluation": {"setup_quality": 80}}, path=self.db)
        plan = {"symbol": "EURUSD", "direction": "BUY", "order_type": "BUY_LIMIT", "entry": 1.1,
                "stop_loss": 1.09, "take_profit": 1.12, "mode": "LIMIT", "target_rr": 2.1}
        _record_reference_plan(event_id, opportunity_time, plan=plan, path=self.db)
        payload = {"allowed_evidence_ids": ["E1"]}
        result = type("R", (), {"model_dump": lambda self, **kwargs: {"strategy": "PULLBACK", "evidence_used": ["E1"]}})()
        with bind_event(event_id, self.db, agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION):
            record_llm_query_started(role=CANONICAL_FILTER_ROLE, policy_id=POLICY_VERSION, payload=payload)
            record_llm_call(schema_name="StrategyAssessment", provider=CANONICAL_PROVIDER, model=CANONICAL_MODEL,
                prompt=MARKET_ANALYST_FILTER_PROMPT, payload=payload, allowed_evidence=["E1"],
                requested_at="2026-01-01T00:00:01Z", result=result,
                metadata={"schema_module": "src.agents.market_analyst", "provider_reported_model": CANONICAL_MODEL},
                agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION)
            record_system_decision(event_id, "WATCH_LONG", validation_status="VALID", decision_stage="MARKET_ANALYSIS",
                agent_role=CANONICAL_FILTER_ROLE, decision_policy_id=POLICY_VERSION, path=self.db)
        now = datetime.now(timezone.utc).isoformat()
        record_trade_context(event_id, {"candidate_status": "ELIGIBLE", "plan_status": "VALID",
            "plan_strategy_source": "TECHNICAL_ONLY_REFERENCE",
            "plan": {"symbol": "EURUSD", "direction": "BUY", "entry": 1.1, "stop_loss": 1.09, "take_profit": 1.12},
            "risk_context": _risk_context(), "market_data_time_utc": "2026-01-02T00:00:00Z",
            "context_known_at_utc": now, "risk_context_known_at_utc": now}, path=self.db)
        row = pair_ledger(self.db)["population"][0]
        self.assertEqual(row["trade_context_status"], "MISSING_OR_FUTURE_DATA_CONTEXT")
        self.assertIsNotNone(row["plan"])
        self.assertIsNone(row["risk_context"])

    def test_duplicate_event_ids_are_no_evaluable(self):
        event = _event("duplicate", decision_time=datetime(2026, 3, 1, tzinfo=timezone.utc))
        report = simulate_paired_policies([event, dict(event)])
        self.assertEqual(report["status"], "NO_EVALUABLE")
        self.assertEqual(report["incomplete_events"][0]["reason"], "DUPLICATE_EVENT_ID")

    def test_economic_comparator_rejects_selected_pair_subset_without_population_report(self):
        selected_pair_only = [_event("selected-only", decision_time=datetime(2026, 3, 1, tzinfo=timezone.utc))]
        result = compare_paired_policies(selected_pair_only)
        self.assertEqual(result["status"], "NO_EVALUABLE")
        self.assertEqual(result["reason_code"], "POPULATION_PROVENANCE_REQUIRED")

    def test_economic_comparator_accepts_full_population_report_including_uncovered_events(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "NO_TRADE"},
                           eligible=False, exclusion_reason="NO_TECHNICAL_CANDIDATE", path=self.db)
        _record_reference_plan(event_id, "2026-01-01T00:00:00Z", status="NO_TECHNICAL_CANDIDATE", path=self.db)
        population_report = pair_ledger(self.db)
        compared = compare_paired_policies(population_report, ledger_path=self.db)
        self.assertEqual(compared["status"], "OFFLINE_RISK_PARITY_SIMULATION")
        self.assertEqual(compared["policies"]["TECHNICAL_ONLY"]["opportunities"], 1)
        self.assertEqual(compared["policies"]["TECHNICAL_ONLY"]["eligible"], 0)
        self.assertFalse(compared["economic_inference_allowed"])

    def test_ambiguous_and_open_outcomes_are_not_counted_as_closed_zero_pnl(self):
        base_time = datetime(2026, 3, 1, tzinfo=timezone.utc)
        ambiguous = _event("ambiguous", decision_time=base_time, net=0)
        ambiguous["outcome"].update({"ambiguous": True, "outcome_status": "AMBIGUOUS",
                                     "net_pnl_eur": None, "gross_pnl_eur": None, "costs_eur": None})
        report = simulate_paired_policies([ambiguous])
        policy = report["policies"]["TECHNICAL_ONLY"]
        self.assertEqual(policy["closed"], 0)
        self.assertEqual(policy["net_pnl_eur"], 0)
        self.assertEqual(policy["ambiguous"], 1)
        self.assertEqual(policy["open_or_unresolved"], 1)

        opened = _event("open", decision_time=base_time)
        opened["outcome"].update({"release_time_utc": None, "known_at_utc": None,
                                  "outcome_status": "OPEN", "net_pnl_eur": None,
                                  "gross_pnl_eur": None, "costs_eur": None})
        report = simulate_paired_policies([opened])
        self.assertEqual(report["status"], "NO_EVALUABLE")
        self.assertEqual(report["policies"]["TECHNICAL_ONLY"]["closed"], 0)
        self.assertEqual(report["policies"]["TECHNICAL_ONLY"]["open_or_unresolved"], 1)

    def test_future_outcome_magnitude_does_not_change_prior_policy_acceptance(self):
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        first = _event("future-result", decision_time=start, release_minutes=60)
        second = _event("next-decision", minute=5, symbol="AUDUSD", decision_time=start, release_minutes=60)
        changed = json.loads(json.dumps([first, second]))
        changed[0]["outcome"]["net_pnl_eur"] = -500
        changed[0]["outcome"]["gross_pnl_eur"] = -499.75
        one = simulate_paired_policies([first, second])
        two = simulate_paired_policies(changed)
        self.assertEqual([row["status"] for row in one["event_results"]["TECHNICAL_ONLY"]],
                         [row["status"] for row in two["event_results"]["TECHNICAL_ONLY"]])
        self.assertEqual(one["sequence"], two["sequence"])
        self.assertFalse(one["economic_inference_allowed"])

    def test_strict_future_outcomes_cannot_precede_signal_or_release(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db)
        with self.assertRaisesRegex(ValueError, "OUTCOME_BEFORE_DECISION"):
            append_outcome(event_id, "2025-12-31T00:00:00Z", {"result": "WIN"}, path=self.db)

    def test_concurrent_writers_keep_every_llm_and_coverage_event(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db)
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(4) as pool:
            results = pool.map(_write_concurrent_call, [(str(self.db), event_id, i) for i in range(8)])
        self.assertEqual(results, ["SAVED"] * 8)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM llm_events").fetchone()[0], 8)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_type='LLM_QUERY_SENT'").fetchone()[0], 8)

    def test_same_time_priority_and_policy_states_are_independent_with_two_slots(self):
        t = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = [
            _event("reject-first", minute=0, symbol="EURUSD", strategy="NONE", net=-2, decision_time=t, quality=90),
            _event("accept-second", minute=1, symbol="USDCAD", action="WATCH_SHORT", strategy="PULLBACK", net=2, decision_time=t, quality=80),
            _event("accept-third", minute=2, symbol="AUDUSD", strategy="BREAKOUT", net=2, decision_time=t, quality=70),
        ]
        report = simulate_paired_policies(events, cost_scenario="CONSERVATIVE", initial_equity_eur=10_000, state_dir=self.root / "risk")
        self.assertEqual(report["risk_config"]["max_open_positions"], 2)
        tech = report["policies"]["TECHNICAL_ONLY"]
        deepseek = report["policies"]["TECHNICAL_PLUS_DEEPSEEK"]
        self.assertEqual(tech["risk_accepted"], 2)
        self.assertEqual(tech["risk_rejected"], 1)
        self.assertEqual(deepseek["risk_accepted"], 2)
        self.assertEqual(deepseek["risk_rejected"], 0)
        self.assertEqual(report["sequence"][0]["event_id"], "reject-first")
        self.assertFalse(report["economic_inference_allowed"])

    def test_daily_loss_is_applied_to_later_opportunities_in_each_policy(self):
        t = datetime(2026, 3, 1, tzinfo=timezone.utc)
        first = _event("daily-loss", minute=0, net=-6, cost=0, decision_time=t, release_minutes=5)
        second = _event("after-lock", minute=10, net=2, cost=0, decision_time=t)
        with patch("src.risk_preflight.MAX_DAILY_LOSS_PERCENT", .06):
            report = simulate_paired_policies([first, second], initial_equity_eur=10_000, state_dir=self.root / "daily")
        reasons = report["policies"]["TECHNICAL_ONLY"]["rejection_reasons"]
        self.assertTrue("DAILY_LOSS_LIMIT_REACHED" in reasons or "DAILY_LOSS_LIMIT_LOCKED" in reasons)
        self.assertEqual(report["policies"]["TECHNICAL_ONLY"]["risk_rejected"], 1)

    def test_currency_exposure_is_enforced_by_shared_risk_engine(self):
        event = _event("usd-exposure", decision_time=datetime(2026, 3, 1, tzinfo=timezone.utc), stop=.00001)
        report = simulate_paired_policies([event], initial_equity_eur=10_000, state_dir=self.root / "exposure")
        result = report["event_results"]["TECHNICAL_ONLY"][0]
        self.assertEqual(result["status"], "RISK_REJECTED")
        self.assertTrue(any("CURRENCY_EXPOSURE_LIMIT_EXCEEDED" in reason for reason in result["reasons"]))

    def test_aggregate_risk_exceeds_limit_using_shared_engine(self):
        context = _risk_context()
        plan = {"symbol": "USDCAD", "direction": "BUY", "volume": .01, "entry": 1.35, "estimated_loss_account": 5}
        position = {"symbol": "EURUSD", "side": "BUY", "volume": .01, "entry": 1.1, "risk_to_sl_account": 199}
        result = evaluate_portfolio_limits(positions=[position], pending_orders=[], proposed_plan=plan,
            symbol_metadata=context["symbol_metadata"], conversion_rates=context["conversion_rates"],
            account_currency="EUR", equity=10_000, max_portfolio_risk_percent=risk_config.MAX_PORTFOLIO_RISK_PERCENT,
            max_currency_exposure_equity_multiple=risk_config.MAX_CURRENCY_EXPOSURE_EQUITY_MULTIPLE)
        self.assertIn("MAX_PORTFOLIO_RISK_EXCEEDED", result["errors"])

    def test_two_persisted_reservations_block_third_proposal(self):
        t = datetime(2026, 3, 1, tzinfo=timezone.utc)
        state_path = self.root / "reservations.json"
        context = _risk_context()
        runtime = {"account": {"currency": "EUR", "equity": 10_000}, "risk_snapshot": {
            "available": True, "realized_net_pnl": 0, "open_risk_account": 0, "pending_risk_account": 0,
            "positions": [], "pending_orders": [], "symbol_metadata": context["symbol_metadata"], "conversion_rates": context["conversion_rates"],
        }}
        plans = [
            {"symbol": symbol, "direction": "BUY", "order_type": "LIMIT", "entry": entry,
             "stop_loss": entry-.001, "take_profit": entry+.002, "volume": .05, "estimated_loss_account": 5}
            for symbol, entry in (("EURUSD",1.1),("USDCAD",1.35),("AUDUSD",.67))
        ]
        first = evaluate_plan_risk(plans[0], runtime, state_path=state_path, now=t)
        second = evaluate_plan_risk(plans[1], runtime, state_path=state_path, now=t+timedelta(seconds=1))
        third = evaluate_plan_risk(plans[2], runtime, state_path=state_path, now=t+timedelta(seconds=2))
        self.assertTrue(first["approved"])
        self.assertTrue(second["approved"])
        self.assertFalse(third["approved"])
        self.assertIn("NO_GLOBAL_SLOT_AVAILABLE", third["errors"])

    def test_expired_reservations_do_not_consume_slots(self):
        t = datetime(2026, 3, 1, tzinfo=timezone.utc)
        state_path = self.root / "expires.json"
        context = _risk_context()
        runtime = {"account": {"currency": "EUR", "equity": 10_000}, "risk_snapshot": {
            "available": True, "realized_net_pnl": 0, "open_risk_account": 0, "pending_risk_account": 0,
            "positions": [], "pending_orders": [], "symbol_metadata": context["symbol_metadata"], "conversion_rates": context["conversion_rates"],
        }}
        plan = {"symbol": "EURUSD", "direction": "BUY", "entry": 1.1, "stop_loss": 1.099, "take_profit": 1.102, "volume": .05, "estimated_loss_account": 5}
        first = evaluate_plan_risk(plan, runtime, state_path=state_path, now=t)
        later = {**plan, "symbol": "AUDUSD", "entry": .67, "stop_loss": .669, "take_profit": .672}
        second = evaluate_plan_risk(later, runtime, state_path=state_path, now=t+timedelta(minutes=risk_config.DAILY_RISK_RESERVATION_TTL_MINUTES+1))
        self.assertTrue(first["approved"])
        self.assertTrue(second["approved"])

    def test_missing_risk_context_or_outcome_is_no_evaluable_not_zero_pnl(self):
        event = _event("missing-risk", risk_context=None)
        event["risk_context"] = None
        report = simulate_paired_policies([event])
        self.assertEqual(report["status"], "NO_EVALUABLE")
        self.assertEqual(report["incomplete_events"][0]["reason"], "RISK_CONTEXT_MISSING")

    def test_anonymous_export_and_ledger_have_no_account_or_credentials(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG", "account_number": "private", "balance": 99999}, path=self.db)
        exported = export_anonymized_ledger(self.db)
        self.assertEqual(len(exported), 1)
        self.assertNotIn("private", json.dumps(exported))
        with sqlite3.connect(self.db) as db:
            snapshot = db.execute("SELECT snapshot_json FROM opportunities WHERE event_id=?", (event_id,)).fetchone()[0]
        self.assertNotIn("account_number", snapshot)
        self.assertNotIn("balance", snapshot)

    def test_taxonomy_outcomes_and_missing_coverage_remain_distinct(self):
        statuses = {"NONE": "MODEL_DECLINED_STRATEGY", "HOLD": "MODEL_STRATEGY_INVALID", "WAIT": "MODEL_STRATEGY_INVALID", "BUY": "MODEL_STRATEGY_INVALID", "SELL": "MODEL_STRATEGY_INVALID"}
        for output, expected in statuses.items():
            self.assertEqual(deepseek_filter_outcome("WATCH_LONG", output, role=CANONICAL_FILTER_ROLE)[1], expected)
        no_coverage = _event("no-coverage", coverage="NO_QUERY_OR_NO_CANONICAL_COVERAGE")
        no_coverage["llm_covered"] = False
        no_coverage["llm_validation"] = "NO_COVERAGE"
        result = simulate_paired_policies([no_coverage])
        self.assertEqual(result["policies"]["TECHNICAL_PLUS_DEEPSEEK"]["rejection_reasons"]["NO_QUERY_OR_NO_CANONICAL_COVERAGE"], 1)


if __name__ == "__main__":
    unittest.main()
