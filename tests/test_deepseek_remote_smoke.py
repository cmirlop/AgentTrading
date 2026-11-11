from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_deepseek_remote_smoke import run_smoke
from scripts.validate_phase1_llm_live import ProbeAnswer
from src.agents import deepseek_client, llm_client
from src.agents.llm_client import LLMProviderError, structured_chat
from src.deepseek_remote_smoke import (
    ALLOWED_EVIDENCE,
    INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION,
    MAX_OUTPUT_TOKENS,
    MAX_TOTAL_COST_USD,
    OUTPUT_PRICE_PEAK_PER_MILLION,
    PRICE_EVIDENCE,
    PROBE_PAYLOAD,
    PROBE_PROMPT,
    authorize_single_request,
    estimate_cost_usd,
    estimate_input_token_upper_bound,
)
from src.event_ledger import bind_event, record_opportunity


UTC = timezone.utc


def _valid_response(content: str | None = None, *, finish_reason: str = "stop", model: str = "deepseek-flash"):
    if content is None:
        content = json.dumps({"status": "PASS", "evidence_used": ["LIVE_PROBE"]})
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(
            prompt_tokens=31,
            completion_tokens=9,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )


class FakeOpenAI:
    def __init__(self, **kwargs):
        self.constructor_kwargs = kwargs
        self.calls = 0
        self.response = _valid_response()
        self.error = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls += 1
        self.request_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class ProbeBudgetTests(unittest.TestCase):
    def test_input_bound_counts_complete_system_schema_and_user_message(self):
        result = estimate_input_token_upper_bound(ProbeAnswer, PROBE_PROMPT, PROBE_PAYLOAD)
        self.assertGreater(result["system_message_utf8_bytes"], len(PROBE_PROMPT.encode("utf-8")))
        self.assertGreater(result["all_message_utf8_bytes"], result["system_message_utf8_bytes"])
        self.assertEqual(result["conservative_input_tokens_upper_bound"], 4 * result["all_message_utf8_bytes"] + 64)

    def test_official_peak_rates_and_worst_case_cost_stay_under_authorized_cap(self):
        self.assertEqual(PRICE_EVIDENCE["model"], "deepseek-flash")
        self.assertEqual(INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION, Decimal("0.30"))
        self.assertEqual(OUTPUT_PRICE_PEAK_PER_MILLION, Decimal("1.20"))
        bound = estimate_input_token_upper_bound(ProbeAnswer, PROBE_PROMPT, PROBE_PAYLOAD)["conservative_input_tokens_upper_bound"]
        worst_case = estimate_cost_usd(bound, MAX_OUTPUT_TOKENS)
        self.assertLessEqual(worst_case, MAX_TOTAL_COST_USD)

    def test_unverified_price_blocks_without_authorizing_request(self):
        bound = estimate_input_token_upper_bound(ProbeAnswer, PROBE_PROMPT, PROBE_PAYLOAD)["conservative_input_tokens_upper_bound"]
        gate, result = authorize_single_request(bound, prices_verified=False)
        self.assertFalse(result["authorized"])
        self.assertEqual(result["reason"], "VERIFIED_API_PRICES_REQUIRED")
        self.assertEqual(gate.requests, 0)

    def test_oversized_input_bound_blocks_before_provider(self):
        gate, result = authorize_single_request(65_537, prices_verified=True)
        self.assertFalse(result["authorized"])
        self.assertEqual(result["reason"], "INPUT_TOKEN_BOUND_EXCEEDED")
        self.assertEqual(gate.requests, 0)

    def test_output_token_limit_and_duplicate_request_block(self):
        from src.research_policy import BudgetGate, BudgetLimits

        gate = BudgetGate(BudgetLimits(1, MAX_OUTPUT_TOKENS, 65_536 + MAX_OUTPUT_TOKENS, MAX_TOTAL_COST_USD, MAX_TOTAL_COST_USD))
        too_many = gate.authorize(
            input_tokens=10,
            max_output_tokens=MAX_OUTPUT_TOKENS + 1,
            price_input_per_million=INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION,
            price_output_per_million=OUTPUT_PRICE_PEAK_PER_MILLION,
            prices_verified=True,
        )
        self.assertFalse(too_many["authorized"])
        self.assertEqual(too_many["reason"], "REQUEST_TOKEN_LIMIT")
        first = gate.authorize(
            input_tokens=10,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            price_input_per_million=INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION,
            price_output_per_million=OUTPUT_PRICE_PEAK_PER_MILLION,
            prices_verified=True,
        )
        second = gate.authorize(
            input_tokens=10,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            price_input_per_million=INPUT_PRICE_PEAK_CACHE_MISS_PER_MILLION,
            price_output_per_million=OUTPUT_PRICE_PEAK_PER_MILLION,
            prices_verified=True,
        )
        self.assertTrue(first["authorized"])
        self.assertFalse(second["authorized"])
        self.assertEqual(second["reason"], "MAX_REQUESTS_REACHED")


class DeepSeekAdapterSingleAttemptTests(unittest.TestCase):
    def _call(self, fake: FakeOpenAI):
        attempts = []
        with patch.dict(os.environ, {
            "DEEPSEEK_API_KEY": "unit-test-secret-do-not-print",
            "DEEPSEEK_MODEL": "deepseek-flash",
            "DEEPSEEK_MAX_TOKENS": "256",
            "DEEPSEEK_REASONING_EFFORT": "low",
            "DEEPSEEK_TIMEOUT_SECONDS": "30",
        }):
            with patch.object(deepseek_client, "OpenAI", return_value=fake) as factory:
                try:
                    answer = deepseek_client.deepseek_structured_chat(
                        ProbeAnswer,
                        PROBE_PROMPT,
                        PROBE_PAYLOAD,
                        model="deepseek-flash",
                        timeout_seconds=30,
                        max_tokens=256,
                        return_metadata=True,
                        on_request_start=lambda: attempts.append("attempted"),
                    )
                    return answer, None, attempts, factory
                except Exception as exc:
                    return None, exc, attempts, factory

    def test_valid_contract_is_pydantic_and_uses_one_no_retry_chat_completion(self):
        fake = FakeOpenAI()
        answer, error, attempts, factory = self._call(fake)
        self.assertIsNone(error)
        self.assertEqual(answer[0], ProbeAnswer(status="PASS", evidence_used=ALLOWED_EVIDENCE))
        self.assertEqual(answer[1]["finish_reason"], "stop")
        self.assertEqual(attempts, ["attempted"])
        self.assertEqual(fake.calls, 1)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(fake.request_kwargs["response_format"], {"type": "json_object"})
        self.assertEqual(fake.request_kwargs["max_tokens"], 256)
        self.assertEqual(fake.request_kwargs["temperature"], 0.0)
        self.assertEqual(fake.request_kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_malformed_json_invented_evidence_and_incomplete_response_fail_once(self):
        cases = [
            (_valid_response("not json"), "JSONDecodeError"),
            (_valid_response(json.dumps({"status": "PASS", "evidence_used": ["FORGED"]})), "ValueError"),
            (_valid_response(finish_reason="length"), "RuntimeError"),
        ]
        for response, expected_type in cases:
            with self.subTest(expected_type=expected_type):
                fake = FakeOpenAI()
                fake.response = response
                _answer, error, attempts, _factory = self._call(fake)
                self.assertIsNotNone(error)
                self.assertEqual(type(error).__name__, expected_type)
                self.assertEqual(attempts, ["attempted"])
                self.assertEqual(fake.calls, 1)

    def test_timeout_401_404_and_429_are_safe_and_single_attempt(self):
        cases = [
            (type("APITimeoutError", (Exception,), {}), None, "LLM_TIMEOUT"),
            (type("AuthenticationError", (Exception,), {"status_code": 401}), None, "LLM_AUTHENTICATION_REJECTED"),
            (type("NotFoundError", (Exception,), {"status_code": 404}), None, "LLM_PROVIDER_MODEL_OR_ENDPOINT_NOT_FOUND"),
            (type("RateLimitError", (Exception,), {"status_code": 429}), None, "LLM_PROVIDER_RATE_LIMITED"),
        ]
        for exception_type, _unused, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                fake = FakeOpenAI()
                fake.error = exception_type("private provider response text")
                _answer, error, attempts, factory = self._call(fake)
                self.assertIsNotNone(error)
                self.assertEqual(str(error), expected_code)
                self.assertEqual(attempts, ["attempted"])
                self.assertEqual(fake.calls, 1)
                self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
                self.assertNotIn("private provider response text", str(error))


class DiagnosticLedgerTests(unittest.TestCase):
    def test_isolated_excluded_probe_persists_query_response_without_trade(self):
        fake = FakeOpenAI()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "probe.sqlite3"
            old_env = {
                "AGENTTRADING_EVENT_DB": str(ledger),
                "RESEARCH_LOG_REQUIRED": "1",
                "LLM_PROVIDER": "deepseek",
                "DEEPSEEK_API_KEY": "unit-test-secret-do-not-print",
                "DEEPSEEK_MODEL": "deepseek-flash",
                "DEEPSEEK_MAX_TOKENS": "256",
                "DEEPSEEK_REASONING_EFFORT": "low",
                "DEEPSEEK_TIMEOUT_SECONDS": "30",
            }
            with patch.dict(os.environ, old_env):
                event_id = record_opportunity(
                    "DIAGNOSTIC",
                    datetime.now(UTC),
                    {"technical_decision": "PROVIDER_CONTRACT_PROBE_ONLY"},
                    eligible=False,
                    exclusion_reason="PROVIDER_CONTRACT_PROBE_NOT_A_MARKET_OPPORTUNITY",
                    strategy_version="REMOTE_DIAGNOSTIC_ONLY",
                    path=ledger,
                )
                with patch.object(deepseek_client, "OpenAI", return_value=fake):
                    with bind_event(event_id, ledger, agent_role="REMOTE_PROVIDER_CONTRACT_PROBE", agent_stage="DIAGNOSTIC_ONLY"):
                        answer, metadata = structured_chat(
                            ProbeAnswer,
                            PROBE_PROMPT,
                            PROBE_PAYLOAD,
                            return_metadata=True,
                        )
            self.assertEqual(answer.status, "PASS")
            self.assertEqual(metadata["provider_reported_model"], "deepseek-flash")
            self.assertEqual(metadata["finish_reason"], "stop")
            import sqlite3
            with sqlite3.connect(ledger) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_type='OPPORTUNITY_ELIGIBLE'").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM llm_events WHERE event_id=? AND pydantic_status='VALID'", (event_id,)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_id=? AND event_type='LLM_QUERY_SENT'", (event_id,)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM coverage_events WHERE event_id=? AND event_type='LLM_RESPONSE_PERSISTED'", (event_id,)).fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM system_events").fetchone()[0], 0)

    def test_blocked_public_report_never_serializes_secret_or_raw_prompt(self):
        secret = "sentinel-secret-must-not-appear"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            report = run_smoke(authorize_single_request=False, run_offline_tests=False)
        serialized = json.dumps(report)
        self.assertEqual(report["live_status"], "DEEPSEEK_REMOTE_LIVE_BLOCKED")
        self.assertEqual(report["calls_attempted"], 0)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(PROBE_PROMPT, serialized)
        self.assertNotIn("DEEPSEEK_API_KEY", serialized)
        self.assertFalse(report["api_key_logged"])
        self.assertFalse(report["operational_signal_created"])
        self.assertEqual(report["orders_sent"], 0)
        self.assertEqual(report["mt5_connections"], 0)


if __name__ == "__main__":
    unittest.main()
