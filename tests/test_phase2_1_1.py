"""Offline Fase 2.1.1 quality, event ledger and pairing regressions."""

from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from pydantic import BaseModel

from src.agents.llm_client import LLMProviderError, structured_chat
from src.event_ledger import append_outcome, bind_event, initialize_ledger, record_llm_call, record_llm_query_started, record_opportunity, record_system_decision
from src.llm_evaluation import compare_paired_policies, pair_ledger
from src.phase2_1_evaluation import classify_engineering_status, classify_llm_value_add, classify_strategy_evidence
from src.research_policy import CANONICAL_FILTER_ROLE, POLICY_VERSION, MARKET_ANALYST_FILTER_PROMPT
from src.spread_quality import audit_spread_values, classify_spread, effective_spread_points
from src.warmup_audit import compare_indicator_windows, ema200_stabilization_bars, indicator_at_window


class _Decision(BaseModel):
    decision: str
    evidence_used: list[str]


def _record_duplicate_opportunity(args):
    database, timestamp = args
    return record_opportunity("EURUSD", timestamp, {"technical_decision": "BUY", "overall_score": 12}, path=database)


def _record_concurrent_llm_event(args):
    database, event_id, index = args
    with bind_event(event_id, database):
        record_llm_call(
            schema_name="ConcurrentFixture", provider="ollama", model="mock",
            prompt=f"prompt-{index}", payload={"index": index}, allowed_evidence=["E1"],
            requested_at="2026-01-01T00:00:01Z",
            result=type("R", (), {"model_dump": lambda self, **kwargs: {"decision": "WAIT", "evidence_used": ["E1"]}})(),
            elapsed_seconds=.01,
        )


class Phase2111Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "events.sqlite3"
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def tearDown(self):
        self.temp.cleanup()

    def _bars(self, rows=900):
        times = pd.date_range("2020-01-01", periods=rows, freq="D", tz="UTC")
        close = [1.1 + i * 0.00001 + (0.001 if i % 13 == 0 else 0) for i in range(rows)]
        return pd.DataFrame({
            "time": times,
            "open": close,
            "high": [value + 0.0005 for value in close],
            "low": [value - 0.0005 for value in close],
            "close": close,
            "tick_volume": [100 + i % 9 for i in range(rows)],
        })

    def test_spread_zero_missing_invalid_and_assumption_are_distinct(self):
        self.assertEqual(classify_spread(0), "OBSERVED_ZERO_UNVERIFIED")
        self.assertEqual(classify_spread(None), "MISSING")
        self.assertEqual(classify_spread(float("nan")), "MISSING")
        self.assertEqual(classify_spread(-1), "INVALID")
        self.assertEqual(effective_spread_points(0, floor_points=5), (5.0, "IMPUTED_ASSUMPTION", True))
        self.assertEqual(effective_spread_points(7, multiplier=2, floor_points=5), (14.0, "OBSERVED_VALID", False))
        with self.assertRaisesRegex(ValueError, "HISTORICAL_SPREAD_MISSING"):
            effective_spread_points(None)

    def test_spread_units_are_not_silently_converted(self):
        frame = pd.Series([0, 1, 2, None, -1])
        audit = audit_spread_values(frame)
        self.assertEqual(audit["quality_counts"]["OBSERVED_ZERO_UNVERIFIED"], 1)
        self.assertEqual(audit["quality_counts"]["MISSING"], 1)
        self.assertEqual(audit["quality_counts"]["INVALID"], 1)
        self.assertFalse(audit["source_values_modified"])

    def test_warmup_cutoff_excludes_unclosed_and_future_bars(self):
        bars = self._bars()
        decision = bars.time.iloc[500] + pd.Timedelta(days=1)
        with_future = pd.concat([bars, pd.DataFrame([{
            "time": decision,
            "open": 99, "high": 100, "low": 98, "close": 99, "tick_volume": 1,
        }])], ignore_index=True)
        first = indicator_at_window(bars, decision, "D1", 400)
        second = indicator_at_window(with_future, decision, "D1", 400)
        self.assertEqual(first, second)
        self.assertLessEqual(pd.Timestamp(first["last_bar_close_utc"]), pd.Timestamp(first["decision_time_utc"]))

    def test_warmup_insufficiency_and_predeclared_stability_criterion(self):
        result = indicator_at_window(self._bars(100), "2021-01-01T00:00:00Z", "D1", 800)
        self.assertEqual(result["warmup_status"], "WARMUP_INSUFFICIENT")
        self.assertGreaterEqual(ema200_stabilization_bars(), 690)
        comparison = compare_indicator_windows(self._bars(), "2022-06-01T00:00:00Z", "D1")
        self.assertTrue(comparison["causal_cutoff_enforced"])
        self.assertIn("price_vs_ema200", comparison["price_relation_changed_from_reference"]["200"])

    def test_event_id_is_stable_deduplicated_and_conflicts_rejected(self):
        timestamp = "2026-01-01T12:00:00Z"
        one = record_opportunity("eurusd", timestamp, {"technical_decision": "BUY", "future_pnl": -50}, path=self.db_path)
        two = record_opportunity("EURUSD", timestamp, {"technical_decision": "BUY", "future_pnl": 100}, path=self.db_path)
        self.assertEqual(one, two)
        with self.assertRaisesRegex(ValueError, "EVENT_ID_PAYLOAD_CONFLICT"):
            record_opportunity("EURUSD", timestamp, {"technical_decision": "SELL"}, path=self.db_path)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)

    def test_event_ledger_is_safe_for_concurrent_duplicate_writers(self):
        timestamp = "2026-02-01T12:00:00Z"
        context = multiprocessing.get_context("spawn")
        with context.Pool(4) as pool:
            ids = pool.map(_record_duplicate_opportunity, [(str(self.db_path), timestamp)] * 8)
        self.assertEqual(len(set(ids)), 1)
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)

    def test_concurrent_llm_events_are_append_only_without_lost_writes(self):
        event_id = record_opportunity("EURUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WAIT"}, path=self.db_path)
        context = multiprocessing.get_context("spawn")
        with context.Pool(4) as pool:
            pool.map(_record_concurrent_llm_event, [(str(self.db_path), event_id, index) for index in range(8)])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM llm_events").fetchone()[0], 8)
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT payload_sha256) FROM llm_events").fetchone()[0], 8)

    def test_outcome_is_appended_separately_and_original_is_immutable(self):
        event_id = record_opportunity("USDCAD", "2026-01-01T00:00:00Z", {"technical_decision": "HOLD"}, path=self.db_path)
        append_outcome(event_id, "2026-01-02T00:00:00Z", {"result": "LOSS", "net_r": -1, "api_key": "must-not-store"}, path=self.db_path)
        with sqlite3.connect(self.db_path) as db:
            before = db.execute("SELECT snapshot_json FROM opportunities WHERE event_id=?", (event_id,)).fetchone()[0]
            after = db.execute("SELECT snapshot_json FROM opportunities WHERE event_id=?", (event_id,)).fetchone()[0]
            outcome = db.execute("SELECT outcome_json FROM outcomes WHERE event_id=?", (event_id,)).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "APPEND_ONLY"):
                db.execute("UPDATE opportunities SET symbol='EURUSD' WHERE event_id=?", (event_id,))
        self.assertEqual(before, after)
        self.assertNotIn("api_key", outcome)

    def test_mock_llm_call_records_valid_decision_and_no_secret(self):
        event_id = record_opportunity("EURUSD", self.timestamp, {"technical_decision": "BUY"}, path=self.db_path)
        response = {"model": "qwen3:4b", "done_reason": "stop", "message": {"content": json.dumps({"decision": "BUY", "evidence_used": ["E1"]})}, "prompt_eval_count": 10, "eval_count": 5}
        class FakeOllama:
            def chat(self, **kwargs):
                return response
        with patch.dict("os.environ", {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3:4b", "DEEPSEEK_API_KEY": "secret-sentinel"}):
            with patch("src.agents.llm_client._ollama_client", return_value=FakeOllama()):
                with bind_event(event_id, self.db_path):
                    result = structured_chat(_Decision, "prompt contents", {"allowed_evidence_ids": ["E1"]})
        self.assertEqual(result.decision, "BUY")
        with sqlite3.connect(self.db_path) as db:
            row = db.execute("SELECT provider, configured_model, pydantic_status, prompt_tokens, completion_tokens FROM llm_events").fetchone()
            content = " ".join(str(cell) for cell in db.execute("SELECT * FROM llm_events").fetchone())
        self.assertEqual(row, ("ollama", "qwen3:4b", "VALID", 10, 5))
        self.assertNotIn("secret-sentinel", content)
        self.assertNotIn("prompt contents", content)

    def test_invalid_evidence_and_provider_error_are_logged_not_operational(self):
        event_id = record_opportunity("EURUSD", self.timestamp, {"technical_decision": "WAIT"}, path=self.db_path)
        class InvalidOllama:
            def chat(self, **kwargs):
                return {"done_reason": "stop", "message": {"content": json.dumps({"decision": "BUY", "evidence_used": ["INVENTED"]})}}
        with patch.dict("os.environ", {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3:4b"}):
            with patch("src.agents.llm_client._ollama_client", return_value=InvalidOllama()):
                with bind_event(event_id, self.db_path):
                    with self.assertRaises(LLMProviderError):
                        structured_chat(_Decision, "prompt", {"allowed_evidence_ids": ["E1"]})
        class BrokenOllama:
            def chat(self, **kwargs):
                raise TimeoutError()
        with patch.dict("os.environ", {"LLM_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3:4b"}):
            with patch("src.agents.llm_client._ollama_client", return_value=BrokenOllama()):
                with bind_event(event_id, self.db_path):
                    with self.assertRaises(LLMProviderError):
                        structured_chat(_Decision, "prompt", {"allowed_evidence_ids": ["E1"]})
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("SELECT pydantic_status, rejection_code, error_code, order_executed FROM llm_events ORDER BY rowid").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "INVALID")
        self.assertEqual(rows[0][1], "LLM_INVALID_EVIDENCE")
        self.assertEqual(rows[1][2], "LLM_TIMEOUT")
        self.assertTrue(all(row[3] == 0 for row in rows))

    def test_pairing_never_invents_llm_decisions_and_requires_provenance(self):
        initialize_ledger(self.db_path)
        empty = pair_ledger(self.db_path)
        self.assertEqual(empty["status"], "NO_VERIFIABLE_PAIRS")
        event_id = record_opportunity("AUDUSD", "2026-01-01T00:00:00Z", {"technical_decision": "HOLD"}, path=self.db_path)
        with sqlite3.connect(self.db_path) as db:
            db.execute("""INSERT INTO llm_events (
                llm_event_id,event_id,schema_name,requested_at_utc,responded_at_utc,provider,configured_model,
                reported_model,prompt_sha256,payload_sha256,allowed_evidence_json,decision_json,pydantic_status,
                rejection_code,latency_ms,prompt_tokens,completion_tokens,error_code,final_system_decision,
                agent_role,agent_stage,decision_policy_id,schema_version,order_executed,created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""", (
                "legacy", event_id, "UnknownSchema", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z", "unknown", "", None,
                "0" * 64, "1" * 64, "[]", '{"decision":"SELL"}', "VALID", None, 10, None, None, None, None,
                "UNREGISTERED_SCHEMA", "UNCLASSIFIED", None, "1.0", "2026-01-01T00:00:02Z",
            ))
        paired = pair_ledger(self.db_path)
        self.assertEqual(paired["status"], "NO_VERIFIABLE_PAIRS")
        self.assertEqual(paired["counts"]["unverified_provenance"], 1)

    def test_uncovered_opportunity_stays_in_technical_population(self):
        event_id = record_opportunity("AUDUSD", "2026-01-01T00:00:00Z", {"technical_decision": "WATCH_LONG"}, path=self.db_path)
        append_outcome(event_id, "2026-01-02T00:00:00Z", {"policy_id": "TRADE_CANDIDATE", "result": "WIN", "net_pnl_eur": 3, "net_r": .3, "release_time_utc": "2026-01-01T01:00:00Z", "cost_scenario": "BASE"}, path=self.db_path)
        joined = pair_ledger(self.db_path)
        self.assertEqual(joined["counts"]["events_without_llm_coverage"], 1)
        self.assertEqual(len(joined["pairs"]), 0)
        self.assertEqual(len(joined["population"]), 1)
        comparison = compare_paired_policies(joined["population"])
        self.assertEqual(comparison["status"], "NO_EVALUABLE")
        self.assertFalse(comparison["economic_inference_allowed"])

    def test_pairing_and_policy_interface_use_same_event_portfolio_constraints(self):
        event_ids = []
        for idx, (symbol, technical, system, pnl, quality) in enumerate((
            ("EURUSD", "WATCH_LONG", "WATCH_LONG", 10.0, 80),
            ("USDCAD", "WATCH_SHORT", "NO_TRADE", -5.0, 70),
        )):
            timestamp = f"2026-01-01T00:0{idx}:00Z"
            event_id = record_opportunity(symbol, timestamp, {"technical_decision": technical, "technical_evaluation": {"setup_quality": quality, "overall_score": quality / 2}}, path=self.db_path)
            event_ids.append(event_id)
            model_strategy = "PULLBACK" if idx == 0 else "NONE"
            payload = {"allowed_evidence_ids": ["E1"]}
            with bind_event(event_id, self.db_path, agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION):
                record_llm_query_started(role=CANONICAL_FILTER_ROLE, policy_id=POLICY_VERSION, payload=payload)
                record_llm_call(schema_name="StrategyAssessment", provider="deepseek", model="deepseek-flash", prompt=MARKET_ANALYST_FILTER_PROMPT, payload=payload, allowed_evidence=["E1"], requested_at=f"2026-01-01T00:0{idx}:01Z", result=type("R", (), {"model_dump": lambda self, **kwargs: {"strategy": model_strategy, "evidence_used": ["E1"]}})(), metadata={"schema_module": "src.agents.market_analyst", "provider_reported_model": "deepseek-flash"}, agent_role=CANONICAL_FILTER_ROLE, agent_stage="STRATEGY_FAMILY_ASSESSMENT", decision_policy_id=POLICY_VERSION)
            record_system_decision(event_id, system, validation_status="VALID", decision_stage="MARKET_ANALYSIS", agent_role=CANONICAL_FILTER_ROLE, decision_policy_id=POLICY_VERSION, path=self.db_path)
            append_outcome(event_id, "2026-01-02T00:00:00Z", {"policy_id": "TRADE_CANDIDATE", "result": "WIN" if pnl > 0 else "LOSS", "net_pnl_eur": pnl, "net_r": pnl / 10, "release_time_utc": f"2026-01-01T01:0{idx}:00Z", "cost_scenario": "BASE"}, path=self.db_path)
        joined = pair_ledger(self.db_path)
        self.assertEqual(joined["counts"]["matching_decisions"], 1)
        comparison = compare_paired_policies(joined["pairs"], max_open_positions=1)
        self.assertEqual(comparison["status"], "NO_EVALUABLE_RISK_CONFIG_OVERRIDE")

    def test_phase2_conclusions_have_explicit_independent_states(self):
        required = {"unit_tests", "baseline_integrity"}
        self.assertEqual(classify_engineering_status({"unit_tests": "PASS", "baseline_integrity": "PASS"}, required), "COMPLETE")
        self.assertEqual(classify_engineering_status({"unit_tests": "PASS"}, required), "INCOMPLETE")
        self.assertEqual(classify_engineering_status({"unit_tests": "FAIL"}, required), "FAILED")
        self.assertEqual(classify_strategy_evidence(confirmatory=False, conservative_expectancy=1, conservative_ci95=[.1, 2]), "INCONCLUSIVE")
        self.assertEqual(classify_llm_value_add(valid_pairs=0, confirmatory=False, paired_mean_difference=None, paired_ci95=None, independent_blocks=0), "INCONCLUSIVE_INSUFFICIENT_DATA")


if __name__ == "__main__":
    unittest.main()
