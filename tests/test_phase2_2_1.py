"""Offline regression checks for the independent technical reference plan."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.event_ledger import (
    bind_event, canonical_hash, initialize_ledger, record_opportunity,
    record_technical_plan, reconcile_ledger,
)
from src.llm_evaluation import compare_paired_policies, pair_ledger
from src.paired_policy_simulator import simulate_paired_policies
from src.technical_reference import (
    ENTRY_MODE, PLAN_SOURCE, POLICY_SHA256, POLICY_VERSION,
    build_technical_reference_plan,
)


def _state(symbol="EURUSD", decision=None, *, spread=8):
    decision = decision or datetime(2026, 4, 1, 12, tzinfo=timezone.utc)
    timeframes = {}
    for timeframe, minutes in {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D1": 1440}.items():
        candle_time = decision - timedelta(minutes=minutes)
        candle = {"time": candle_time.isoformat().replace("+00:00", "Z"),
                  "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1,
                  "tick_volume": 100, "spread": spread if timeframe == "M5" else 8}
        timeframes[timeframe] = {"recent_candles": [candle], "indicators": {}}
    return {"symbol": symbol, "analysis_time": decision.isoformat().replace("+00:00", "Z"),
            "timeframes": timeframes}


def _plan(symbol="EURUSD", direction="BUY"):
    entry = {"EURUSD": 1.1, "USDCAD": 1.35, "AUDUSD": .67}[symbol]
    distance = .001
    return {
        "symbol": symbol, "direction": direction,
        "order_type": "BUY_LIMIT" if direction == "BUY" else "SELL_LIMIT",
        "entry": entry, "stop_loss": entry - distance if direction == "BUY" else entry + distance,
        "take_profit": entry + distance * 2.1 if direction == "BUY" else entry - distance * 2.1,
        "mode": "LIMIT", "target_rr": 2.1,
    }


def _technical_result(event_id, decision_time, plan=None, *, status="TECHNICAL_PLAN_VALID"):
    decision = datetime.fromisoformat(decision_time.replace("Z", "+00:00")).astimezone(timezone.utc)
    return {
        "schema_version": "1.0", "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256, "plan_source": PLAN_SOURCE,
        "status": status, "symbol": (plan or {}).get("symbol", "EURUSD"),
        "decision_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "constructed_at_utc": (decision + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "maximum_market_data_time_utc": decision.isoformat().replace("+00:00", "Z"),
        "technical_decision": "WATCH_LONG" if plan else "NO_TRADE",
        "direction": (plan or {}).get("direction"), "plan": plan,
        "plan_sha256": canonical_hash(plan) if plan else None,
        "reason": "FIXTURE", "missing_fields": [],
        "spread_quality": "OBSERVED_VALID", "confirmatory_eligible": False,
        "technical_snapshot": {"bias": "BULLISH", "alignment": "ALIGNED",
                               "setup_quality": 80, "overall_score": 40,
                               "available_evidence_ids": ["E1"]},
    }


class Phase221TechnicalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "events.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def _build(self, state=None, *, bias="BULLISH", quality=80, alignment="ALIGNED", historical=None):
        decision = state["analysis_time"] if state else _state()["analysis_time"]
        with patch("src.technical_reference.build_compact_snapshot", return_value={}) as compact, \
             patch("src.technical_reference.evaluate_technical_state", return_value={
                 "bias": bias, "alignment": alignment, "setup_quality": quality,
                 "overall_score": 40, "timeframe_scores": {"M15": 80},
             }) as evaluation, \
             patch("src.technical_reference.build_historical_trade_plan", return_value=historical or {
                 "status": "VALID", "order_type": "BUY_LIMIT" if bias == "BULLISH" else "SELL_LIMIT",
                 "entry": 1.1, "stop_loss": 1.099 if bias == "BULLISH" else 1.101,
                 "take_profit": 1.1021 if bias == "BULLISH" else 1.0979, "target_rr": 2.1,
             }) as planner:
            result = build_technical_reference_plan(
                "EURUSD", state or _state(), decision,
                constructed_at_utc=(datetime.fromisoformat(decision.replace("Z", "+00:00")) + timedelta(seconds=2)).isoformat(),
            )
        return result, compact, evaluation, planner

    def test_reference_reuses_existing_65_limit_plan_builder_without_llm(self):
        state = _state()
        with patch("src.technical_reference.build_compact_snapshot", return_value={}), \
             patch("src.technical_reference.evaluate_technical_state", return_value={
                 "bias": "BULLISH", "alignment": "ALIGNED", "setup_quality": 80,
                 "overall_score": 40, "timeframe_scores": {},
             }), \
             patch("src.technical_reference.build_historical_trade_plan", return_value={
                 "status": "VALID", "order_type": "BUY_LIMIT", "entry": 1.1,
                 "stop_loss": 1.099, "take_profit": 1.1021, "target_rr": 2.1,
             }) as planner, \
             patch("src.agents.llm_client.structured_chat", side_effect=AssertionError("LLM must not be called")):
            result = build_technical_reference_plan("EURUSD", state, state["analysis_time"],
                constructed_at_utc="2026-04-01T12:00:02Z")
        self.assertEqual(result["status"], "TECHNICAL_PLAN_VALID")
        self.assertEqual(result["plan_source"], "TECHNICAL_ONLY_REFERENCE")
        self.assertEqual(result["policy_version"], "TECHNICAL_REFERENCE_V1")
        self.assertEqual(result["plan"]["mode"], ENTRY_MODE)
        self.assertEqual(result["plan"]["target_rr"], 2.1)
        self.assertEqual(planner.call_args.args[1:3], ("BUY", "LIMIT"))

    def test_plan_and_hash_are_deterministic_and_do_not_depend_on_model_answer(self):
        state = _state()
        with patch("src.technical_reference.build_compact_snapshot", return_value={}), \
             patch("src.technical_reference.evaluate_technical_state", return_value={
                 "bias": "BEARISH", "alignment": "ALIGNED", "setup_quality": 85,
                 "overall_score": 44, "timeframe_scores": {},
             }), \
             patch("src.technical_reference.build_historical_trade_plan", return_value={
                 "status": "VALID", "order_type": "SELL_LIMIT", "entry": 1.1,
                 "stop_loss": 1.101, "take_profit": 1.0979, "target_rr": 2.1,
             }):
            first = build_technical_reference_plan("EURUSD", state, state["analysis_time"], constructed_at_utc="2026-04-01T12:00:02Z")
            # The builder has no provider/result input: BUY, SELL, HOLD or timeout cannot change its plan.
            second = build_technical_reference_plan("EURUSD", state, state["analysis_time"], constructed_at_utc="2026-04-01T12:00:02Z")
        self.assertEqual(first, second)
        self.assertEqual(first["direction"], "SELL")
        self.assertEqual(first["plan_sha256"], canonical_hash(first["plan"]))

    def test_all_timeframes_are_checked_for_future_closed_candles(self):
        state = _state()
        state["timeframes"]["H4"]["recent_candles"][0]["time"] = "2026-04-01T08:01:00Z"
        result, *_ = self._build(state)
        self.assertEqual(result["status"], "TECHNICAL_PLAN_NOT_EVALUABLE")
        self.assertIn("FUTURE_H4_CANDLE", result["reason"])

    def test_missing_spread_is_not_silently_replaced_with_zero(self):
        state = _state()
        del state["timeframes"]["M5"]["recent_candles"][0]["spread"]
        result, *_ = self._build(state)
        self.assertEqual(result["status"], "TECHNICAL_PLAN_NOT_EVALUABLE")
        self.assertIn("M5_SPREAD_MISSING", result["reason"])

    def test_zero_spread_is_explicitly_unverified_and_not_confirmatory(self):
        state = _state(spread=0)
        result, *_ = self._build(state)
        self.assertEqual(result["spread_quality"], "OBSERVED_ZERO_UNVERIFIED")
        self.assertFalse(result["confirmatory_eligible"])

    def test_non_candidate_and_historical_plan_rejection_are_retained_with_reasons(self):
        state = _state()
        no_candidate, *_ = self._build(state, quality=64)
        self.assertEqual(no_candidate["status"], "NO_TECHNICAL_CANDIDATE")
        rejected, *_ = self._build(state, historical={"status": "REJECTED", "error": "BUY_LIMIT_NOT_BELOW_MARKET"})
        self.assertEqual(rejected["status"], "TECHNICAL_PLAN_NOT_EVALUABLE")
        self.assertEqual(rejected["reason"], "BUY_LIMIT_NOT_BELOW_MARKET")

    def test_plan_record_is_append_only_idempotent_and_reconciliation_requires_every_event(self):
        timestamp = "2026-04-01T12:00:00Z"
        event = record_opportunity("EURUSD", timestamp, {"technical_decision": "WATCH_LONG"}, path=self.db)
        result = _technical_result(event, timestamp, _plan())
        first = record_technical_plan(event, result, path=self.db)
        duplicate = record_technical_plan(event, result, path=self.db)
        self.assertEqual(first.status, "SAVED")
        self.assertEqual(duplicate.status, "DUPLICATE_IDENTICAL")
        with sqlite3.connect(self.db) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE technical_plans SET eligibility_reason='changed' WHERE event_id=?", (event,))
        report = reconcile_ledger(self.db)
        self.assertIn("ELIGIBLE_OPPORTUNITY_WITHOUT_TRADE_CONTEXT", report["discrepancies"])

    def test_migration_adds_new_ledger_tables_and_request_ids_without_dropping_rows(self):
        old = self.db
        with sqlite3.connect(old) as db:
            db.executescript("""
                CREATE TABLE opportunities(event_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
                    opportunity_id_version TEXT NOT NULL, symbol TEXT NOT NULL, decision_time_utc TEXT NOT NULL,
                    opportunity_ordinal INTEGER NOT NULL, snapshot_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                    strategy_version TEXT, config_sha256 TEXT, code_revision TEXT, created_at_utc TEXT NOT NULL);
                CREATE TABLE llm_events(llm_event_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,schema_name TEXT NOT NULL,
                    requested_at_utc TEXT NOT NULL,responded_at_utc TEXT NOT NULL,provider TEXT NOT NULL,
                    configured_model TEXT NOT NULL,reported_model TEXT,prompt_sha256 TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,allowed_evidence_json TEXT NOT NULL,decision_json TEXT,
                    pydantic_status TEXT NOT NULL,rejection_code TEXT,latency_ms INTEGER,prompt_tokens INTEGER,
                    completion_tokens INTEGER,error_code TEXT,final_system_decision TEXT,order_executed INTEGER NOT NULL DEFAULT 0,
                    created_at_utc TEXT NOT NULL);
                CREATE TABLE system_events(system_event_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,created_at_utc TEXT NOT NULL,
                    final_decision TEXT NOT NULL,decision_stage TEXT NOT NULL,validation_status TEXT NOT NULL,
                    rejection_code TEXT,order_executed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE coverage_events(coverage_event_id TEXT PRIMARY KEY,event_id TEXT,event_type TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,agent_role TEXT,decision_policy_id TEXT,payload_sha256 TEXT,
                    status_code TEXT,eligible INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE outcomes(outcome_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,known_at_utc TEXT NOT NULL,
                    outcome_schema_version TEXT NOT NULL,outcome_json TEXT NOT NULL,created_at_utc TEXT NOT NULL);
                CREATE TABLE trade_contexts(context_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,context_type TEXT NOT NULL,
                    schema_version TEXT NOT NULL,context_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,created_at_utc TEXT NOT NULL);
                INSERT INTO opportunities VALUES('legacy','2.0','v1','EURUSD','2026-01-01T00:00:00Z',0,'{}','hash',NULL,NULL,NULL,'2026-01-01T00:00:00Z');
            """)
        initialize_ledger(old)
        with sqlite3.connect(old) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(llm_events)")}
            self.assertIn("request_id", columns)
            self.assertIn("technical_plans", {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM opportunities WHERE event_id='legacy'").fetchone()[0], 1)

    def test_economic_comparator_revalidates_ledger_and_rejects_fabricated_or_stale_report(self):
        timestamp = "2026-04-01T12:00:00Z"
        event = record_opportunity("EURUSD", timestamp, {"technical_decision": "NO_TRADE"},
                                   eligible=False, exclusion_reason="NO_TECHNICAL_CANDIDATE", path=self.db)
        record_technical_plan(event, _technical_result(event, timestamp, None, status="NO_TECHNICAL_CANDIDATE"), path=self.db)
        report = pair_ledger(self.db)
        self.assertNotEqual(compare_paired_policies({"status": "PASS", "population": report["population"]}, ledger_path=self.db)["status"], "OFFLINE_RISK_PARITY_SIMULATION")
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO coverage_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("new-row", event, None, "SYNTHETIC_AUDIT_MARKER", timestamp, None, None, None, None, 0))
        self.assertEqual(compare_paired_policies(report, ledger_path=self.db)["status"], "NO_EVALUABLE_SOURCE_OR_REPORT_MISMATCH")

    def test_ledger_reconciliation_rejects_duplicate_request_identifiers(self):
        timestamp = "2026-04-01T12:00:00Z"
        event = record_opportunity("EURUSD", timestamp, {"technical_decision": "NO_TRADE"}, eligible=False, path=self.db)
        record_technical_plan(event, _technical_result(event, timestamp, None, status="NO_TECHNICAL_CANDIDATE"), path=self.db)
        with sqlite3.connect(self.db) as db:
            db.execute("INSERT INTO coverage_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("duplicate-request", event, "request-1", "LLM_QUERY_SENT", timestamp,
                 "TEST_ROLE", None, "hash", "SENT", 0))
            db.execute("INSERT INTO coverage_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("duplicate-request-2", event, "request-1", "LLM_QUERY_SENT", timestamp,
                 "TEST_ROLE", None, "hash", "SENT", 0))
        report = reconcile_ledger(self.db)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("DUPLICATE_OR_MISSING_QUERY_ID", report["discrepancies"])

    def test_paired_policies_require_same_common_plan_and_ignore_persist_time_for_slot_release(self):
        start = datetime(2026, 4, 1, 11, 30, tzinfo=timezone.utc)
        from test_phase2_2 import _event
        first = _event("slot-1", decision_time=start, symbol="EURUSD", release_minutes=30)
        second = _event("slot-2", decision_time=start, symbol="AUDUSD", release_minutes=30)
        third = _event("after-close", minute=35, decision_time=start, symbol="USDCAD", release_minutes=30)
        events = [first, second, third]
        baseline = simulate_paired_policies(events, initial_equity_eur=10_000)
        changed = json.loads(json.dumps(events))
        for event in changed[:2]:
            close = datetime.fromisoformat(event["outcome"]["persisted_at_utc"].replace("Z", "+00:00"))
            event["outcome"]["persisted_at_utc"] = (close + timedelta(minutes=20)).isoformat()
        delayed = simulate_paired_policies(changed, initial_equity_eur=10_000)
        self.assertTrue(baseline["common_plan_parity"]["all_technical_candidates_share_one_plan_record"])
        self.assertEqual([row["status"] for row in baseline["event_results"]["TECHNICAL_ONLY"]],
                         [row["status"] for row in delayed["event_results"]["TECHNICAL_ONLY"]])
        self.assertEqual(baseline["event_results"]["TECHNICAL_ONLY"][2]["status"], "SIMULATED_ACCEPTED")

    def test_persistence_time_does_not_make_unobserved_close_release_early(self):
        start = datetime(2026, 4, 1, 11, 30, tzinfo=timezone.utc)
        from test_phase2_2 import _event
        first = _event("open-1", decision_time=start, symbol="EURUSD", release_minutes=30)
        second = _event("open-2", decision_time=start, symbol="AUDUSD", release_minutes=30)
        third = _event("before-observation", minute=35, decision_time=start, symbol="USDCAD", release_minutes=30)
        for event in (first, second):
            close = datetime.fromisoformat(event["outcome"]["known_at_utc"].replace("Z", "+00:00"))
            observed = close + timedelta(minutes=10)
            event["outcome"]["known_at_utc"] = observed.isoformat()
            event["outcome"]["outcome_observed_at_utc"] = observed.isoformat()
            event["outcome"]["persisted_at_utc"] = (observed + timedelta(minutes=1)).isoformat()
        report = simulate_paired_policies([first, second, third], initial_equity_eur=10_000)
        self.assertEqual(report["event_results"]["TECHNICAL_ONLY"][2]["status"], "RISK_REJECTED")

    def test_no_technical_candidate_keeps_explicit_plan_status_not_a_fake_order(self):
        result, *_ = self._build(quality=64)
        self.assertEqual(result["status"], "NO_TECHNICAL_CANDIDATE")
        self.assertIsNone(result["plan"])
        self.assertIsNone(result["plan_sha256"])


if __name__ == "__main__":
    unittest.main()
